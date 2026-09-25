from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models import Group, Message, Sender
from private_chat.access import CatchUpMessage
from private_chat.handler import PrivateChatHandler
from whatsapp.jid import JID

MODULE = "private_chat.handler"
USER = "972501111111@s.whatsapp.net"
GROUPS = [
    Group(group_jid="a@g.us", group_name="Founders"),
    Group(group_jid="b@g.us", group_name="Tech"),
]


def _message(text: str) -> Message:
    return Message(
        message_id="m1",
        chat_jid=USER,
        sender_jid=USER,
        text=text,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def handler():
    session = MagicMock()
    session.get = AsyncMock(return_value=Sender(jid=USER))
    session.commit = AsyncMock()
    whatsapp = AsyncMock()
    whatsapp.get_my_jid = AsyncMock(
        return_value=JID(user="972500000000", server="s.whatsapp.net")
    )
    settings = MagicMock(dm_daily_quota=20, model_name="test")
    h = PrivateChatHandler(session, whatsapp, AsyncMock(), settings)
    h.send_message = AsyncMock()  # type: ignore[method-assign]
    h.knowledge.respond = AsyncMock()  # type: ignore[method-assign]
    return h


def _sent(handler) -> str:
    return handler.send_message.await_args.args[1]


@pytest.fixture(autouse=True)
def under_quota():
    with patch(f"{MODULE}.requests_last_24h", AsyncMock(return_value=1)) as quota:
        yield quota


class TestQuota:
    @pytest.mark.asyncio
    async def test_over_quota_gets_a_message_and_no_llm(self, handler, under_quota):
        under_quota.return_value = 21
        await handler(_message("מה החלטנו?"), GROUPS)
        assert "מכסה" in _sent(handler)
        handler.knowledge.respond.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exactly_at_quota_is_still_allowed(self, handler, under_quota):
        # The current message is already stored and counted, so 20 of 20 is fine.
        under_quota.return_value = 20
        await handler(_message("מה החלטנו?"), GROUPS)
        handler.knowledge.respond.assert_awaited_once()


class TestRouting:
    @pytest.mark.asyncio
    async def test_help_lists_the_users_groups(self, handler):
        await handler(_message("עזרה"), GROUPS)
        text = _sent(handler)
        assert "Founders" in text and "Tech" in text
        handler.knowledge.respond.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_question_is_scoped_to_allowed_groups_privately(self, handler):
        await handler(_message("מה החלטנו על המיטאפ?"), GROUPS)
        kwargs = handler.knowledge.respond.await_args.kwargs
        assert kwargs["scope"].group_jids == ["a@g.us", "b@g.us"]
        assert kwargs["scope"].private is True
        assert kwargs["scope"].group_names == {"a@g.us": "Founders", "b@g.us": "Tech"}
        assert kwargs["chat_jid"] == USER
        assert kwargs["history_limit"] == 6

    @pytest.mark.asyncio
    async def test_search_without_terms_asks_for_them(self, handler):
        with patch(f"{MODULE}.keyword_search", AsyncMock()) as search:
            await handler(_message("חפש"), GROUPS)
        search.assert_not_awaited()
        assert "מה לחפש" in _sent(handler)

    @pytest.mark.asyncio
    async def test_search_is_scoped_hides_bot_and_numbers(self, handler):
        now = datetime.now(timezone.utc)
        hits = [
            (
                Message(
                    message_id="1",
                    chat_jid="a@g.us",
                    group_jid="a@g.us",
                    sender_jid=USER,
                    text="call me 972503333333",
                    timestamp=now,
                ),
                0.9,
            ),
            (
                Message(
                    message_id="2",
                    chat_jid="a@g.us",
                    group_jid="a@g.us",
                    sender_jid="972500000000@s.whatsapp.net",
                    text="bot reply",
                    timestamp=now,
                ),
                0.8,
            ),
        ]
        with (
            patch(f"{MODULE}.keyword_search", AsyncMock(return_value=hits)) as search,
            patch(
                f"{MODULE}.display_names",
                AsyncMock(return_value={"972501111111": "Dana"}),
            ),
        ):
            await handler(_message("חפש call"), GROUPS)

        assert search.await_args is not None
        assert search.await_args.args[2] == ["a@g.us", "b@g.us"]
        text = _sent(handler)
        assert "Dana" in text and "Founders" in text
        assert "bot reply" not in text
        assert "972" not in text


class TestCatchUp:
    @pytest.mark.asyncio
    async def test_summarizes_redacts_and_advances_marker(self, handler):
        msgs = {
            "a@g.us": [
                CatchUpMessage(
                    "1", datetime.now(timezone.utc), USER, "we ship on Sunday"
                )
            ]
        }
        agent = MagicMock()
        agent.run = AsyncMock(
            return_value=MagicMock(output="*Founders* ship Sunday, per 972501111111")
        )
        with (
            patch(f"{MODULE}.fetch_catch_up", AsyncMock(return_value=msgs)),
            patch(
                f"{MODULE}.display_names",
                AsyncMock(return_value={"972501111111": "Dana"}),
            ),
            patch(f"{MODULE}.Agent", return_value=agent),
        ):
            await handler(_message("מה פספסתי"), GROUPS)

        assert _sent(handler) == "*Founders* ship Sunday, per Dana"
        prompt = agent.run.await_args.args[0]
        assert "972" not in prompt and "# Founders" in prompt
        sender = handler.session.get.return_value
        assert sender.last_catchup_at is not None
        handler.session.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_marker_not_advanced_when_llm_fails(self, handler):
        msgs = {"a@g.us": [CatchUpMessage("1", datetime.now(timezone.utc), USER, "x")]}
        agent = MagicMock()
        agent.run = AsyncMock(side_effect=RuntimeError("model down"))
        with (
            patch(f"{MODULE}.fetch_catch_up", AsyncMock(return_value=msgs)),
            patch(f"{MODULE}.display_names", AsyncMock(return_value={})),
            patch(f"{MODULE}.Agent", return_value=agent),
        ):
            await handler(_message("מה פספסתי"), GROUPS)

        assert handler.session.get.return_value.last_catchup_at is None
        assert "השתבש" in _sent(handler)  # the user still gets an answer

    @pytest.mark.asyncio
    async def test_nothing_new(self, handler):
        with patch(f"{MODULE}.fetch_catch_up", AsyncMock(return_value={})):
            await handler(_message("מה פספסתי"), GROUPS)
        assert "אין" in _sent(handler)


class TestFailures:
    @pytest.mark.asyncio
    async def test_question_failure_still_replies(self, handler):
        handler.knowledge.respond.side_effect = RuntimeError("llm down")
        await handler(_message("שאלה?"), GROUPS)
        assert "השתבש" in _sent(handler)

    @pytest.mark.asyncio
    async def test_search_failure_still_replies(self, handler):
        with patch(
            f"{MODULE}.keyword_search", AsyncMock(side_effect=RuntimeError("db down"))
        ):
            await handler(_message("חפש פייתון"), GROUPS)
        assert "השתבש" in _sent(handler)

    @pytest.mark.asyncio
    async def test_typing_indicator_is_shown_for_llm_paths(self, handler):
        await handler(_message("שאלה?"), GROUPS)
        actions = [
            c.args[0].action
            for c in handler.whatsapp.send_chat_presence.await_args_list
        ]
        assert actions == ["start", "stop"]
