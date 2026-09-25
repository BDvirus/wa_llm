from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models import Group
from summarize_and_send_to_groups import summarize_and_send_to_group
from whatsapp.jid import JID

_OLD_SYNC = datetime(2026, 1, 1, 12, 0, 0)


def _session_returning(messages: list) -> MagicMock:
    result = MagicMock()
    result.all.return_value = messages
    session = MagicMock()
    session.exec = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


def _whatsapp() -> AsyncMock:
    client = AsyncMock()
    client.get_my_jid = AsyncMock(return_value=JID(user="bot", server="s.whatsapp.net"))
    return client


def _group() -> Group:
    return Group(group_jid="123@g.us", group_name="Test", last_summary_sync=_OLD_SYNC)


def _summary(text: str = "summary") -> MagicMock:
    result = MagicMock()
    result.output = text
    return result


@pytest.mark.asyncio
async def test_reports_too_few_messages_and_leaves_window():
    session = _session_returning([MagicMock()] * 14)
    whatsapp = _whatsapp()
    group = _group()

    outcome = await summarize_and_send_to_group(MagicMock(), session, whatsapp, group)

    assert outcome == "too_few_messages"
    whatsapp.send_message.assert_not_awaited()
    session.commit.assert_not_awaited()
    assert group.last_summary_sync == _OLD_SYNC


@pytest.mark.asyncio
async def test_reports_summarize_failed_and_leaves_window():
    session = _session_returning([MagicMock()] * 15)
    whatsapp = _whatsapp()
    group = _group()

    with patch(
        "summarize_and_send_to_groups.summarize",
        AsyncMock(side_effect=RuntimeError("llm down")),
    ):
        outcome = await summarize_and_send_to_group(
            MagicMock(), session, whatsapp, group
        )

    assert outcome == "summarize_failed"
    whatsapp.send_message.assert_not_awaited()
    assert group.last_summary_sync == _OLD_SYNC


@pytest.mark.asyncio
async def test_reports_send_failed_but_still_advances_window():
    # Documents existing behaviour: the `finally` advances the window even
    # when sending fails, so the summary is lost. The outcome must say so.
    session = _session_returning([MagicMock()] * 15)
    whatsapp = _whatsapp()
    whatsapp.send_message = AsyncMock(side_effect=RuntimeError("gowa down"))
    group = _group()

    with patch(
        "summarize_and_send_to_groups.summarize", AsyncMock(return_value=_summary())
    ):
        outcome = await summarize_and_send_to_group(
            MagicMock(), session, whatsapp, group
        )

    assert outcome == "send_failed"
    session.commit.assert_awaited_once()
    assert group.last_summary_sync > _OLD_SYNC


@pytest.mark.asyncio
async def test_reports_sent_and_advances_window():
    session = _session_returning([MagicMock()] * 15)
    whatsapp = _whatsapp()
    group = _group()

    with patch(
        "summarize_and_send_to_groups.summarize",
        AsyncMock(return_value=_summary("hello group")),
    ):
        outcome = await summarize_and_send_to_group(
            MagicMock(), session, whatsapp, group
        )

    assert outcome == "sent"
    whatsapp.send_message.assert_awaited_once()
    assert group.last_summary_sync > _OLD_SYNC
    session.commit.assert_awaited_once()
