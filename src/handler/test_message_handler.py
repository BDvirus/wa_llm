from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

import handler as handler_module
from handler import MessageHandler
from gowa_sdk.webhooks import WebhookEnvelope
from models import Message
from test_utils.mock_session import AsyncSessionMock
from whatsapp import SendMessageRequest
from whatsapp.jid import JID
from config import Settings


@pytest.fixture(autouse=True)
def clear_dedupe_cache():
    # The dedupe cache is module-global and now covers private chats too;
    # tests reuse message ids, so each test starts clean.
    handler_module._processing_cache.clear()
    yield
    handler_module._processing_cache.clear()


@pytest.fixture
def mock_whatsapp():
    client = AsyncMock()
    client.send_message = AsyncMock()
    client.get_my_jid = AsyncMock(return_value=JID(user="bot", server="s.whatsapp.net"))
    return client


@pytest.fixture
def mock_embedding_client():
    client = AsyncMock()
    return client


@pytest.fixture
def mock_settings():
    return Mock(spec=Settings, model_name="test-model", dm_autoreply_enabled=False)


@pytest.mark.asyncio
async def test_message_handler_dm_opt_out(
    mock_session: AsyncSessionMock,
    mock_whatsapp: AsyncMock,
    mock_embedding_client: AsyncMock,
    mock_settings: Mock,
):
    # Create handler instance
    handler = MessageHandler(
        mock_session, mock_whatsapp, mock_embedding_client, mock_settings
    )

    # Mock store_message to return our test message
    test_message = Message(
        message_id="1",
        chat_jid="user@s.whatsapp.net",
        sender_jid="user@s.whatsapp.net",  # DM: sender == chat (usually, but logic checks message.group)
        text="opt-out",
        timestamp=datetime.now(timezone.utc),
    )
    # Ensure message.group is None for DM check
    # In the code: if message and not message.group:
    # Message model has a 'group' relationship. We can just set it to None or rely on default.
    # But wait, store_message returns a Message object.

    # We need to mock store_message because __call__ calls it.
    # However, store_message is an async method on the instance.
    handler.store_message = AsyncMock(return_value=test_message)

    # Create a dummy payload
    payload = WebhookEnvelope.model_validate(
        {
            "event": "message",
            "payload": {
                "id": "msg_opt_out",
                "chat_id": "user@s.whatsapp.net",
                "from": "user@s.whatsapp.net",
                "from_name": "User",
                "timestamp": datetime.now(timezone.utc),
                "body": "opt-out",
            },
        }
    )

    # Fix mock response for send_message
    mock_response = AsyncMock()
    mock_response.results.message_id = "response_id"
    mock_whatsapp.send_message.return_value = mock_response

    await handler(payload)

    # Verify upsert was called (which calls execute)
    mock_session.execute.assert_called()

    # Verify confirmation message
    mock_whatsapp.send_message.assert_called_with(
        SendMessageRequest(
            phone="user@s.whatsapp.net",
            message="You have been opted out. You will no longer be tagged in summaries and answers.",
            reply_message_id=None,
        )
    )


@pytest.mark.asyncio
async def test_message_handler_dm_opt_in(
    mock_session: AsyncSessionMock,
    mock_whatsapp: AsyncMock,
    mock_embedding_client: AsyncMock,
    mock_settings: Mock,
):
    handler = MessageHandler(
        mock_session, mock_whatsapp, mock_embedding_client, mock_settings
    )

    test_message = Message(
        message_id="1",
        chat_jid="user@s.whatsapp.net",
        sender_jid="user@s.whatsapp.net",
        text="opt-in",
        timestamp=datetime.now(timezone.utc),
    )
    handler.store_message = AsyncMock(return_value=test_message)

    payload = WebhookEnvelope.model_validate(
        {
            "event": "message",
            "payload": {
                "id": "msg_opt_in",
                "chat_id": "user@s.whatsapp.net",
                "from": "user@s.whatsapp.net",
                "from_name": "User",
                "timestamp": datetime.now(timezone.utc),
                "body": "opt-in",
            },
        }
    )

    # Mock existing opt-out record
    from models import OptOut

    opt_out = OptOut(jid="user@s.whatsapp.net")
    mock_session._storage[("OptOut", "user@s.whatsapp.net")] = opt_out

    mock_response = AsyncMock()
    mock_response.results.message_id = "response_id"
    mock_whatsapp.send_message.return_value = mock_response

    await handler(payload)

    # Verify delete was called
    mock_session.delete.assert_called_with(opt_out)
    mock_session.commit.assert_called()

    # Verify confirmation message
    mock_whatsapp.send_message.assert_called_with(
        SendMessageRequest(
            phone="user@s.whatsapp.net",
            message="You have been opted in. You will now be tagged in summaries and answers.",
            reply_message_id=None,
        )
    )


@pytest.mark.asyncio
async def test_message_handler_dm_status(
    mock_session: AsyncSessionMock,
    mock_whatsapp: AsyncMock,
    mock_embedding_client: AsyncMock,
    mock_settings: Mock,
):
    handler = MessageHandler(
        mock_session, mock_whatsapp, mock_embedding_client, mock_settings
    )

    test_message = Message(
        message_id="1",
        chat_jid="user@s.whatsapp.net",
        sender_jid="user@s.whatsapp.net",
        text="status",
        timestamp=datetime.now(timezone.utc),
    )
    handler.store_message = AsyncMock(return_value=test_message)

    payload = WebhookEnvelope.model_validate(
        {
            "event": "message",
            "payload": {
                "id": "msg_status",
                "chat_id": "user@s.whatsapp.net",
                "from": "user@s.whatsapp.net",
                "from_name": "User",
                "timestamp": datetime.now(timezone.utc),
                "body": "status",
            },
        }
    )

    # Mock get to return None (opted in)
    mock_session.get.return_value = None

    mock_response = AsyncMock()
    mock_response.results.message_id = "response_id"
    mock_whatsapp.send_message.return_value = mock_response

    await handler(payload)

    # Verify status message
    mock_whatsapp.send_message.assert_called_with(
        SendMessageRequest(
            phone="user@s.whatsapp.net",
            message="You are currently opted in.",
            reply_message_id=None,
        )
    )


def _dm_payload(chat: str = "user@s.whatsapp.net", **extra) -> WebhookEnvelope:
    return WebhookEnvelope.model_validate(
        {
            "event": "message",
            "payload": {
                "id": "x",
                "chat_id": chat,
                "from": "user@s.whatsapp.net",
                **extra,
            },
        }
    )


def _dm(
    text: str, chat: str = "user@s.whatsapp.net", message_id: str = "dm1"
) -> Message:
    return Message(
        message_id=message_id,
        chat_jid=chat,
        sender_jid="user@s.whatsapp.net",
        text=text,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def private_handler(mock_session, mock_whatsapp, mock_embedding_client, mock_settings):
    h = MessageHandler(
        mock_session, mock_whatsapp, mock_embedding_client, mock_settings
    )
    h.private_chat = AsyncMock()  # type: ignore[method-assign]
    h.send_message = AsyncMock()  # type: ignore[method-assign]
    return h


@pytest.mark.asyncio
async def test_member_private_message_goes_to_private_chat(
    private_handler, monkeypatch
):
    from models import Group

    groups = [Group(group_jid="a@g.us")]
    lookup = AsyncMock(return_value=groups)
    monkeypatch.setattr(handler_module, "allowed_groups", lookup)
    private_handler.store_message = AsyncMock(return_value=_dm("מה החלטנו?"))

    await private_handler(_dm_payload(from_lid="111:4@lid"))

    private_handler.private_chat.assert_awaited_once()
    assert private_handler.private_chat.await_args.args[1] == groups
    # Both identities, normalized (device suffix dropped from the LID).
    assert lookup.await_args is not None
    assert lookup.await_args.args[1] == {"user@s.whatsapp.net", "111@lid"}


@pytest.mark.asyncio
async def test_non_member_gets_existing_behaviour_only(
    private_handler, mock_settings, monkeypatch
):
    monkeypatch.setattr(handler_module, "allowed_groups", AsyncMock(return_value=[]))
    mock_settings.dm_autoreply_enabled = True
    mock_settings.dm_autoreply_message = "auto"
    private_handler.store_message = AsyncMock(return_value=_dm("hello"))

    await private_handler(_dm_payload())

    private_handler.private_chat.assert_not_awaited()
    private_handler.send_message.assert_awaited_once()
    assert private_handler.send_message.await_args.args[:2] == (
        "user@s.whatsapp.net",
        "auto",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat", ["status@broadcast", "123456@newsletter", "999@broadcast"]
)
async def test_non_person_chats_never_reach_private_chat(
    private_handler, monkeypatch, chat
):
    lookup = AsyncMock(return_value=["anything"])
    monkeypatch.setattr(handler_module, "allowed_groups", lookup)
    private_handler.store_message = AsyncMock(return_value=_dm("hi", chat=chat))

    await private_handler(_dm_payload(chat=chat))

    lookup.assert_not_awaited()
    private_handler.private_chat.assert_not_awaited()
    private_handler.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_messages_from_the_bot_itself_are_ignored(private_handler, monkeypatch):
    lookup = AsyncMock(return_value=["anything"])
    monkeypatch.setattr(handler_module, "allowed_groups", lookup)
    private_handler.store_message = AsyncMock(return_value=_dm("echo"))

    await private_handler(_dm_payload(is_from_me=True))

    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedupe_covers_private_messages(private_handler, monkeypatch):
    monkeypatch.setattr(handler_module, "allowed_groups", AsyncMock(return_value=["g"]))
    private_handler.store_message = AsyncMock(return_value=_dm("q", message_id="same"))

    await private_handler(_dm_payload())
    await private_handler(_dm_payload())  # gowa's retry

    private_handler.private_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_reply_chat_must_belong_to_the_authorized_identity(
    private_handler, monkeypatch
):
    # A forged webhook: the victim's identity, but the attacker's chat.
    lookup = AsyncMock(return_value=["victim-group"])
    monkeypatch.setattr(handler_module, "allowed_groups", lookup)
    attacker = "972509999999@s.whatsapp.net"
    private_handler.store_message = AsyncMock(return_value=_dm("q", chat=attacker))

    await private_handler(_dm_payload(chat=attacker))

    lookup.assert_not_awaited()
    private_handler.private_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_lid_chat_matches_the_senders_lid(private_handler, monkeypatch):
    monkeypatch.setattr(handler_module, "allowed_groups", AsyncMock(return_value=["g"]))
    private_handler.store_message = AsyncMock(return_value=_dm("q", chat="111@lid"))

    await private_handler(_dm_payload(chat="111@lid", from_lid="111:4@lid"))

    private_handler.private_chat.assert_awaited_once()
