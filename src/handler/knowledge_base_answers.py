import logging
from dataclasses import dataclass
from typing import List

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
from voyageai.client_async import AsyncClient

from models import Group, Message
from whatsapp import WhatsAppClient
from whatsapp.jid import parse_jid
from utils.chat_text import chat2text
from utils.opt_out import get_opt_out_map
from utils.names import display_names
from utils.redact import phone_users, redact_phones
from utils.voyage_embed_text import voyage_embed_text
from search.hybrid_search import format_search_results_for_prompt, hybrid_search
from .base_handler import BaseHandler
from config import Settings
from services.prompt_manager import prompt_manager


# Creating an object
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnswerScope:
    """Which groups an answer may draw on, and how it will be shown.

    group_jids is required and non-empty: an unscoped search would read every
    group's knowledge. group_names labels topics when more than one group is
    in scope. private=True means the answer goes to a private chat, so phone
    numbers are replaced with names in both the prompt and the output.
    """

    group_jids: list[str]
    group_names: dict[str, str] | None = None
    private: bool = False


class KnowledgeBaseAnswers(BaseHandler):
    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        embedding_client: AsyncClient,
        settings: Settings,
    ):
        self.settings = settings
        super().__init__(session, whatsapp, embedding_client)

    async def __call__(self, message: Message):
        """Answer a question asked in a group chat."""
        if message.text is None:
            logger.warning(f"Received message with no text from {message.sender_jid}")
            return
        group_jids = await self._group_scope(message)
        if not group_jids:
            logger.warning(
                "No group scope for message %s in %s; not answering",
                message.message_id,
                message.chat_jid,
            )
            return
        await self.respond(
            chat_jid=message.chat_jid,
            query=message.text,
            sender_jid=message.sender_jid,
            scope=AnswerScope(group_jids=group_jids),
        )

    async def _group_scope(self, message: Message) -> list[str]:
        """The message's group plus its community groups.

        Falls back to loading the group by group_jid: a Message built in code
        (not loaded from the session) has no `group` relationship, which used
        to make the search silently unscoped.
        """
        group = message.group
        if group is None and message.group_jid:
            group = await self.session.get(Group, message.group_jid)
        if group is None:
            return []
        group_jids = [group.group_jid]
        if group.community_keys:
            related = await group.get_related_community_groups(self.session)
            group_jids.extend(g.group_jid for g in related)
        return group_jids

    async def respond(
        self,
        *,
        chat_jid: str,
        query: str,
        sender_jid: str,
        scope: AnswerScope,
        history_limit: int = 7,
    ) -> None:
        """Answer `query` within `scope` and send the reply to `chat_jid`."""
        stmt = (
            select(Message)
            .where(Message.chat_jid == chat_jid)
            .order_by(desc(Message.timestamp))
            .limit(history_limit)
        )
        history: list[Message] = list((await self.session.exec(stmt)).all())
        text = await self.answer(
            query=query, history=history, sender_jid=sender_jid, scope=scope
        )
        await self.send_message(chat_jid, text)

    async def answer(
        self,
        *,
        query: str,
        history: list[Message],
        sender_jid: str,
        scope: AnswerScope,
    ) -> str:
        if not scope.group_jids:
            raise ValueError("answer requires at least one group in scope")

        history_jids = {m.sender_jid for m in history} | {sender_jid}
        history_map = await get_opt_out_map(self.session, list(history_jids))
        rephrased_result = await self.rephrasing_agent(
            (await self.whatsapp.get_my_jid()).user, query, history, history_map
        )
        embedded_question = (
            await voyage_embed_text(self.embedding_client, [rephrased_result.output])
        )[0]

        search_results = await hybrid_search(
            session=self.session,
            query=query,
            query_embedding=embedded_question,
            group_jids=scope.group_jids,
            vector_limit=10,
            messages_per_topic=5,
        )

        # Names are resolved after the search, over everyone who can appear in
        # the prompt - not just the chat history.
        result_jids = {
            m.sender_jid for r in search_results for m in r.messages if m.sender_jid
        }
        all_jids = history_jids | result_jids
        if scope.private:
            users = {parse_jid(j).user for j in all_jids}
            for r in search_results:
                users |= phone_users(f"{r.topic.subject} {r.topic.summary}")
            name_map = await display_names(self.session, users)
        else:
            name_map = await get_opt_out_map(self.session, list(all_jids))

        formatted_topics = format_search_results_for_prompt(
            search_results,
            name_map,
            group_names=scope.group_names if len(scope.group_jids) > 1 else None,
            private=scope.private,
        )
        generation_result = await self.generation_agent(
            query,
            formatted_topics,
            sender_jid,
            history,
            name_map,
            private=scope.private,
        )
        output = generation_result.output
        if scope.private:
            # Belt and braces: the prompt was already redacted.
            output = redact_phones(output, name_map)

        logger.info(
            "RAG Query Results: sender=%s groups=%s private=%s topics=%d messages=%d "
            "distances=%s question=%r rephrased=%r",
            parse_jid(sender_jid).user,
            scope.group_jids,
            scope.private,
            len(search_results),
            sum(len(r.messages) for r in search_results),
            [r.vector_distance for r in search_results],
            query,
            rephrased_result.output,
        )
        return output

    @retry(
        wait=wait_random_exponential(min=1, max=30),
        stop=stop_after_attempt(6),
        before_sleep=before_sleep_log(logger, logging.DEBUG),
        reraise=True,
    )
    async def generation_agent(
        self,
        query: str,
        topics: str,  # receives pre-formatted topics
        sender: str,
        history: List[Message],
        opt_out_map: dict[str, str],
        private: bool = False,
    ) -> AgentRunResult[str]:
        agent = Agent(
            model=self.settings.model_name,
            system_prompt=prompt_manager.render("rag.j2", private=private),
        )

        sender_user = parse_jid(sender).user
        sender_display = opt_out_map.get(sender_user, f"@{sender_user}")

        prompt_template = f"""
        {sender_display}: {query}
        
        # Recent chat history:
        {chat2text(history, opt_out_map)}
        
        # Related Topics:
        {topics}
        """
        if private:
            # The model never sees a phone number it could repeat.
            prompt_template = redact_phones(prompt_template, opt_out_map)

        return await agent.run(prompt_template)

    @retry(
        wait=wait_random_exponential(min=1, max=30),
        stop=stop_after_attempt(6),
        before_sleep=before_sleep_log(logger, logging.DEBUG),
        reraise=True,
    )
    async def rephrasing_agent(
        self,
        my_jid: str,
        query: str,
        history: List[Message],
        opt_out_map: dict[str, str],
    ) -> AgentRunResult[str]:
        rephrased_agent = Agent(
            model=self.settings.model_name,
            system_prompt=prompt_manager.render("rephrase.j2", my_jid=my_jid),
        )

        # We obviously need to translate the question and turn the question vebality to a title / summary text to make it closer to the questions in the rag
        return await rephrased_agent.run(
            f"{query}\n\n## Recent chat history:\n {chat2text(history, opt_out_map)}"
        )
