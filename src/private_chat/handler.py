"""Private chats with the bot: questions, search, and "what did I miss"."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable

from pydantic_ai import Agent
from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from utils.relative_time import format_relative
from config import Settings
from handler.base_handler import BaseHandler
from handler.knowledge_base_answers import AnswerScope, KnowledgeBaseAnswers
from models import Group, Sender
from search.hybrid_search import keyword_search
from services.prompt_manager import prompt_manager
from utils.names import display_names
from utils.redact import UNKNOWN_PERSON, phone_users, redact_phones
from whatsapp import WhatsAppClient
from whatsapp.jid import parse_jid
from gowa_sdk import SendChatPresenceRequest

from .access import CatchUpMessage, fetch_catch_up, requests_last_24h
from .commands import Command, parse_command
from .formatting import SearchHit, format_search_hits, help_text

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 6
SEARCH_RESULTS = 10
FIRST_CATCH_UP = timedelta(hours=24)
MAX_CATCH_UP = timedelta(days=7)
FAILURE_REPLY = "משהו השתבש בדרך. נסו שוב בעוד כמה דקות 🙏"


class PrivateChatHandler(BaseHandler):
    """Serves one private-chat request from an authorized group member.

    Every reply goes to message.chat_jid - history and quota are keyed by it,
    so mixing in sender_jid (phone vs @lid) would split the conversation.
    """

    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        embedding_client: AsyncClient,
        settings: Settings,
    ):
        self.settings = settings
        self.knowledge = KnowledgeBaseAnswers(
            session, whatsapp, embedding_client, settings
        )
        super().__init__(session, whatsapp, embedding_client)

    async def __call__(self, message, groups: list[Group]) -> None:
        chat = message.chat_jid
        quota = self.settings.dm_daily_quota
        used = await requests_last_24h(
            self.session, chat_jid=chat, sender_jid=message.sender_jid
        )
        # The current message is already stored, so it is part of `used`.
        if used > quota:
            await self.send_message(
                chat, f"הגעת למכסה של {quota} בקשות ביממה. אפשר לנסות שוב מחר 🙂"
            )
            return

        names = {g.group_jid: g.group_name or g.group_jid for g in groups}
        command, argument = parse_command(message.text or "")
        if command is Command.HELP:
            await self.send_message(chat, help_text(list(names.values()), quota))
        elif command is Command.SEARCH:
            await self._with_llm(chat, self._search(chat, argument, names))
        elif command is Command.CATCH_UP:
            await self._with_llm(chat, self._catch_up(message, names))
        else:
            await self._with_llm(
                chat,
                self.knowledge.respond(
                    chat_jid=chat,
                    query=argument,
                    sender_jid=message.sender_jid,
                    scope=AnswerScope(
                        group_jids=list(names), group_names=names, private=True
                    ),
                    history_limit=HISTORY_LIMIT,
                ),
            )

    async def _with_llm(self, chat: str, work: Awaitable[None]) -> None:
        """Show a typing indicator around slow work (LLM or search), and always
        reply on failure.

        The webhook dedupe has already marked this message, so if the work
        raised, gowa's retry would be skipped and the user would get nothing.
        """
        await self._presence(chat, "start")
        try:
            await work
        except Exception:
            logger.exception("Private chat request failed for %s", chat)
            await self.send_message(chat, FAILURE_REPLY)
        finally:
            await self._presence(chat, "stop")

    async def _presence(self, chat: str, action: str) -> None:
        try:
            await self.whatsapp.send_chat_presence(
                SendChatPresenceRequest(phone=chat, action=action)  # type: ignore[arg-type]
            )
        except Exception:
            logger.debug("Chat presence %s failed for %s", action, chat, exc_info=True)

    async def _bot_identity(self) -> str | None:
        try:
            return (await self.whatsapp.get_my_jid()).normalize_str()
        except Exception:
            return None

    async def _search(self, chat: str, query: str, names: dict[str, str]) -> None:
        if not query:
            await self.send_message(chat, "מה לחפש? למשל: חפש קורס פייתון")
            return
        bot = await self._bot_identity()
        found = await keyword_search(
            self.session, query, list(names), limit=SEARCH_RESULTS * 2
        )
        messages = [m for m, _ in found if m.sender_jid != bot][:SEARCH_RESULTS]

        users = {parse_jid(m.sender_jid).user for m in messages}
        for m in messages:
            users |= phone_users(m.text or "")
        people = await display_names(self.session, users)
        hits = [
            SearchHit(
                group_name=names.get(m.group_jid or "", ""),
                sender_name=people.get(parse_jid(m.sender_jid).user, UNKNOWN_PERSON),
                timestamp=m.timestamp,
                text=redact_phones(m.text or "", people),
            )
            for m in messages
        ]
        await self.send_message(chat, format_search_hits(hits, query))

    async def _catch_up(self, message, names: dict[str, str]) -> None:
        chat = message.chat_jid
        now = datetime.now(timezone.utc)
        sender = await self.session.get(Sender, message.sender_jid)
        since = (sender.last_catchup_at if sender else None) or (now - FIRST_CATCH_UP)
        since = max(since, now - MAX_CATCH_UP)

        by_group = await fetch_catch_up(
            self.session,
            list(names),
            since=since,
            bot_identity=await self._bot_identity(),
        )
        if not by_group:
            await self.send_message(
                chat, f"אין הודעות חדשות בקבוצות שלך מאז {format_relative(since, now)}."
            )
            await self._mark_caught_up(sender, now)
            return

        people = await display_names(self.session, _users_in(by_group))
        prompt = redact_phones(_catch_up_prompt(by_group, names, people, now), people)
        agent = Agent(
            model=self.settings.model_name,
            system_prompt=prompt_manager.render("private_catchup.j2"),
            output_type=str,
        )
        result = await agent.run(prompt)
        await self.send_message(chat, redact_phones(result.output, people))
        # Only after a successful send: a failed run must not skip messages.
        await self._mark_caught_up(sender, now)

    async def _mark_caught_up(self, sender: Sender | None, now: datetime) -> None:
        if sender is None:
            return
        sender.last_catchup_at = now
        self.session.add(sender)
        # Deliberately mid-request: the reply is already sent, so the marker
        # must stick even if something later in the request fails.
        await self.session.commit()


def _users_in(by_group: dict[str, list[CatchUpMessage]]) -> set[str]:
    users: set[str] = set()
    for messages in by_group.values():
        for m in messages:
            users.add(parse_jid(m.sender_jid).user)
            users |= phone_users(m.text)
    return users


def _catch_up_prompt(
    by_group: dict[str, list[CatchUpMessage]],
    names: dict[str, str],
    people: dict[str, str],
    now: datetime,
) -> str:
    sections = []
    for group_jid, messages in by_group.items():
        lines = [
            f"{format_relative(m.timestamp, now)} "
            f"{people.get(parse_jid(m.sender_jid).user, UNKNOWN_PERSON)}: {m.text}"
            for m in messages
        ]
        sections.append(f"# {names.get(group_jid, group_jid)}\n" + "\n".join(lines))
    return "\n\n".join(sections)
