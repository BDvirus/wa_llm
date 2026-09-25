import asyncio
import logging
from datetime import datetime
from typing import Literal

from pydantic_ai import Agent
from pydantic_ai.agent import AgentRunResult
from sqlmodel import select, desc
from sqlmodel.ext.asyncio.session import AsyncSession
from tenacity import (
    retry,
    wait_random_exponential,
    stop_after_attempt,
    before_sleep_log,
)

from config import Settings
from models import Group, Message
from services.prompt_manager import prompt_manager
from utils.chat_text import chat2text
from utils.opt_out import get_opt_out_map
from whatsapp import WhatsAppClient, SendMessageRequest

logger = logging.getLogger(__name__)

# What happened on one group's summary run. Callers that only need to fire and
# forget (the batch endpoint) can ignore it; the admin UI reports it verbatim.
SummaryOutcome = Literal["too_few_messages", "summarize_failed", "send_failed", "sent"]

MIN_MESSAGES_TO_SUMMARIZE = 15


@retry(
    wait=wait_random_exponential(min=1, max=30),
    stop=stop_after_attempt(6),
    before_sleep=before_sleep_log(logger, logging.DEBUG),
    reraise=True,
)
async def summarize(
    session: AsyncSession, settings: Settings, group_name: str, messages: list[Message]
) -> AgentRunResult[str]:
    agent = Agent(
        model=settings.model_name,
        # TODO: move to jinja?
        system_prompt=prompt_manager.render("quick_summary.j2", group_name=group_name),
        output_type=str,
    )

    # Get opt-out map for all senders in the history
    all_jids = {m.sender_jid for m in messages}
    opt_out_map = await get_opt_out_map(session, list(all_jids))

    return await agent.run(chat2text(messages, opt_out_map))


async def summarize_and_send_to_group(
    settings: Settings, session, whatsapp: WhatsAppClient, group: Group
) -> SummaryOutcome:
    resp = await session.exec(
        select(Message)
        .where(Message.group_jid == group.group_jid)
        .where(Message.timestamp >= group.last_summary_sync)
        .where(Message.sender_jid != (await whatsapp.get_my_jid()).normalize_str())
        .order_by(desc(Message.timestamp))
    )
    messages: list[Message] = resp.all()

    if len(messages) < MIN_MESSAGES_TO_SUMMARIZE:
        logging.info("Not enough messages to summarize in group %s", group.group_name)
        return "too_few_messages"

    try:
        result = await summarize(
            session, settings, group.group_name or "group", messages
        )
    except Exception as e:
        logging.error("Error summarizing group %s: %s", group.group_name, e)
        return "summarize_failed"

    outcome: SummaryOutcome = "sent"
    try:
        await whatsapp.send_message(
            SendMessageRequest(phone=group.group_jid, message=result.output)
        )

        # Send the summary to the community groups
        community_groups = await group.get_related_community_groups(session)
        for cg in community_groups:
            await whatsapp.send_message(
                SendMessageRequest(phone=cg.group_jid, message=result.output)
            )

    except Exception as e:
        logging.error("Error sending message to group %s: %s", group.group_name, e)
        outcome = "send_failed"

    finally:
        # Update the group with the new last_summary_sync. Note this runs even
        # when sending failed, so that summary is lost - reported as send_failed.
        group.last_summary_sync = datetime.now()
        session.add(group)
        await session.commit()

    return outcome


async def summarize_and_send_to_groups(
    settings: Settings, session: AsyncSession, whatsapp: WhatsAppClient
):
    groups = await session.exec(select(Group).where(Group.managed == True))  # noqa: E712 https://stackoverflow.com/a/18998106
    tasks = [
        summarize_and_send_to_group(settings, session, whatsapp, group)
        for group in list(groups.all())
    ]
    errs = await asyncio.gather(*tasks, return_exceptions=True)
    for e in errs:
        if isinstance(e, BaseException):
            logging.error("Error syncing group: %s", e)
