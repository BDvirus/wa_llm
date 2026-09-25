import asyncio
import logging

from cachetools import TTLCache
from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from config import Settings
from handler.router import Router
from handler.whatsapp_group_link_spam import WhatsappGroupLinkSpamHandler
from handler.kb_qa import KBQAHandler
from gowa_sdk.webhooks import WebhookEnvelope, WebhookMessagePayload
from private_chat.access import allowed_groups
from private_chat.handler import PrivateChatHandler
from whatsapp import WhatsAppClient
from whatsapp.jid import (
    DefaultUserServer,
    HiddenUserServer,
    JIDParseError,
    normalize_identity,
    parse_jid,
)
from .base_handler import BaseHandler
from models import Message, OptOut
from urllib.parse import urlparse
import re

logger = logging.getLogger(__name__)

_PERSON_SERVERS = (DefaultUserServer, HiddenUserServer)


def _is_person_chat(chat_jid: str) -> bool:
    """True for one-to-one chats (phone JID or LID), not status/broadcast/newsletter."""
    try:
        return parse_jid(chat_jid).server in _PERSON_SERVERS
    except JIDParseError:
        return False


# In-memory processing guard: 4 minutes TTL to prevent duplicate handling
_processing_cache = TTLCache(maxsize=1000, ttl=4 * 60)
_processing_lock = asyncio.Lock()


class MessageHandler(BaseHandler):
    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        embedding_client: AsyncClient,
        settings: Settings,
    ):
        self.router = Router(session, whatsapp, embedding_client, settings)
        self.whatsapp_group_link_spam = WhatsappGroupLinkSpamHandler(
            session, whatsapp, embedding_client, settings
        )
        self.kb_qa_handler = KBQAHandler(session, whatsapp, embedding_client, settings)
        self.private_chat = PrivateChatHandler(
            session, whatsapp, embedding_client, settings
        )
        self.settings = settings
        super().__init__(session, whatsapp, embedding_client)

    async def __call__(self, payload: WebhookEnvelope):
        message = await self.store_message(payload)

        # Persist immediately: a failure later in the handler must not roll
        # back the stored message (it would be lost for the daily summary).
        await self.session.commit()

        # ignore messages that don't exist or don't have text
        if not message or not message.text:
            return

        # Ignore messages sent by the bot itself (also as echoed with a LID sender)
        if payload.payload.get("is_from_me"):
            return
        my_jid = await self.whatsapp.get_my_jid()
        if message.sender_jid == my_jid.normalize_str():
            return

        if message.sender_jid.endswith("@lid"):
            logging.info(
                f"Received message from {message.sender_jid}: {payload.model_dump_json()}"
            )

        # In-memory dedupe, before any branch: gowa retries after its 10s
        # timeout, and a slow (LLM) reply would otherwise be sent twice.
        if message.message_id:
            async with _processing_lock:
                if message.message_id in _processing_cache:
                    logging.info(
                        f"Message {message.message_id} already in processing cache; skipping."
                    )
                    return
                _processing_cache[message.message_id] = True

        # Not in a group: a private chat - but only with a person. Status
        # updates, broadcast lists and newsletters also have no group, and a
        # reply to their chat_jid would be posted publicly.
        if message.group_jid is None:
            if _is_person_chat(message.chat_jid):
                await self._handle_private(message, payload)
            return

        # Check for /kb_qa command (super admin only)
        # This does not have to be a managed group
        if message.group and message.text.startswith("/kb_qa "):
            if message.chat_jid not in self.settings.qa_test_groups:
                logger.warning(
                    f"QA command attempted from non-whitelisted group: {message.chat_jid}"
                )
                return  # Silent failure
            # Check if sender is a QA tester
            if message.sender_jid not in self.settings.qa_testers:
                logger.warning(f"Unauthorized /kb_qa attempt from {message.sender_jid}")
                return  # Silent failure

            await self.kb_qa_handler(message)
            return

        # ignore messages from unmanaged groups
        if message and message.group and not message.group.managed:
            return

        mentioned = message.has_mentioned(my_jid)
        if mentioned:
            await self.router(message)
            return

        if (
            message.group
            and message.group.notify_on_spam
            and self._contains_whatsapp_group_link(message.text)
        ):
            await self.whatsapp_group_link_spam(message)
            return

    async def _handle_private(self, message: Message, payload: WebhookEnvelope) -> None:
        text = message.text or ""
        command = text.strip().lower()
        if command == "opt-out":
            await self.handle_opt_out(message)
            return
        if command == "opt-in":
            await self.handle_opt_in(message)
            return
        if command == "status":
            await self.handle_opt_status(message)
            return

        data = WebhookMessagePayload.model_validate(payload.payload)
        identities = {
            i
            for i in (
                normalize_identity(message.sender_jid),
                normalize_identity(data.from_lid),
            )
            if i
        }
        # The webhook body is not signed yet, so chat_id and from/from_lid are
        # independent claims. Replies go to chat_id: it must be the same person
        # we authorize, or a forged body could route one member's answers to
        # someone else's chat.
        chat_identity = normalize_identity(message.chat_jid)
        if chat_identity not in identities:
            logger.warning(
                "Private chat %s does not match sender identities %s; not answering",
                message.chat_jid,
                sorted(identities),
            )
            groups = []
        else:
            groups = await allowed_groups(self.session, identities)
        if groups:
            await self.private_chat(message, groups)
            return

        # Not a member of any group open to private questions: unchanged behaviour.
        if self.settings.dm_autoreply_enabled:
            await self.send_message(
                message.chat_jid,
                self.settings.dm_autoreply_message,
                message.message_id,
            )

    def _contains_whatsapp_group_link(self, text: str) -> bool:
        """
        Return True if the given text contains a WhatsApp group invite link
        hosted on chat.whatsapp.com.
        """
        if not text:
            return False

        # Simple regex to extract candidate HTTP(S) URLs from text.
        url_pattern = re.compile(r"https?://[^\s]+", re.IGNORECASE)
        for match in url_pattern.finditer(text):
            candidate = match.group(0)
            parsed = urlparse(candidate)
            if (
                parsed.scheme in ("http", "https")
                and parsed.hostname == "chat.whatsapp.com"
            ):
                return True
        return False

    async def handle_opt_out(self, message: Message):
        opt_out = await self.session.get(OptOut, message.sender_jid)
        if not opt_out:
            opt_out = OptOut(jid=message.sender_jid)
            await self.upsert(opt_out)
            await self.send_message(
                message.chat_jid,
                "You have been opted out. You will no longer be tagged in summaries and answers.",
            )
        else:
            await self.send_message(
                message.chat_jid,
                "You are already opted out.",
            )

    async def handle_opt_in(self, message: Message):
        opt_out = await self.session.get(OptOut, message.sender_jid)
        if opt_out:
            await self.session.delete(opt_out)
            await self.session.commit()
            await self.send_message(
                message.chat_jid,
                "You have been opted in. You will now be tagged in summaries and answers.",
            )
        else:
            await self.send_message(
                message.chat_jid,
                "You are already opted in.",
            )

    async def handle_opt_status(self, message: Message):
        opt_out = await self.session.get(OptOut, message.sender_jid)
        status = "opted out" if opt_out else "opted in"
        await self.send_message(
            message.chat_jid,
            f"You are currently {status}.",
        )
