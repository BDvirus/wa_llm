"""Integration tests: the aggregate SQL is the logic, so it runs against real Postgres."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from admin.queries import (
    GroupNotFoundError,
    SpamNeedsOwnerError,
    fetch_community_index,
    fetch_group_row,
    fetch_group_rows,
    set_community_keys,
    set_managed,
    set_notify_on_spam,
)
from models import Group, KBTopic, Message, Sender

pytestmark = pytest.mark.integration

BOT = "999@s.whatsapp.net"
ALICE = "111@s.whatsapp.net"

# Windows and message times sit days apart, so a few hours of host-timezone
# skew (see the note in queries.py) can't flip any count.
WINDOW = datetime.now() - timedelta(days=10)
BEFORE = datetime.now(timezone.utc) - timedelta(days=20)
AFTER = datetime.now(timezone.utc) - timedelta(days=2)


async def _seed(session: AsyncSession) -> None:
    session.add_all(
        [
            Sender(jid=BOT),
            Sender(jid=ALICE),
            Group(
                group_jid="busy@g.us",
                group_name="Busy",
                owner_jid=ALICE,
                managed=True,
                last_ingest=WINDOW,
                last_summary_sync=WINDOW,
            ),
            Group(
                group_jid="quiet@g.us",
                group_name=None,
                managed=False,
                last_ingest=WINDOW,
                last_summary_sync=WINDOW,
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            Message(
                message_id="old",
                chat_jid="busy@g.us",
                sender_jid=ALICE,
                group_jid="busy@g.us",
                text="old",
                timestamp=BEFORE,
            ),
            Message(
                message_id="new1",
                chat_jid="busy@g.us",
                sender_jid=ALICE,
                group_jid="busy@g.us",
                text="new",
                timestamp=AFTER,
            ),
            Message(
                message_id="new2",
                chat_jid="busy@g.us",
                sender_jid=ALICE,
                group_jid="busy@g.us",
                text="new",
                timestamp=AFTER,
            ),
            Message(
                message_id="bot",
                chat_jid="busy@g.us",
                sender_jid=BOT,
                group_jid="busy@g.us",
                text="reply",
                timestamp=AFTER,
            ),
        ]
    )
    for i in range(2):
        session.add(
            KBTopic(
                id=f"t{i}",
                group_jid="busy@g.us",
                speakers="",
                subject="s",
                summary="s",
                embedding=[0.0] * 1024,
                start_time=AFTER,
            )
        )
    await session.commit()


async def _row(session: AsyncSession, jid: str, bot: str = BOT):
    row = await fetch_group_row(session, jid, bot)
    assert row is not None
    return row


async def test_counts_respect_windows_and_exclude_the_bot(db_session: AsyncSession):
    await _seed(db_session)

    busy = await _row(db_session, "busy@g.us")

    assert busy.total == 4
    assert busy.pending_ingest == 2  # new1, new2 - not "old", not the bot's reply
    assert busy.pending_summary == 2
    assert busy.last_activity is not None


async def test_topics_do_not_multiply_message_counts(db_session: AsyncSession):
    await _seed(db_session)

    busy = await _row(db_session, "busy@g.us")

    assert busy.topics == 2
    assert busy.total == 4  # would be 8 if topics were joined directly


async def test_group_without_messages_still_listed(db_session: AsyncSession):
    await _seed(db_session)

    quiet = await _row(db_session, "quiet@g.us")

    assert (quiet.total, quiet.pending_ingest, quiet.topics) == (0, 0, 0)
    assert quiet.last_activity is None
    assert quiet.display_name == "quiet@g.us"


async def test_unknown_bot_overcounts_rather_than_zeroing(db_session: AsyncSession):
    await _seed(db_session)

    busy = await _row(db_session, "busy@g.us", bot="")

    assert busy.pending_ingest == 3  # includes the bot's reply - the safe direction


async def test_rows_list_managed_groups_first(db_session: AsyncSession):
    await _seed(db_session)

    rows = await fetch_group_rows(db_session, BOT)

    assert [r.group_jid for r in rows] == ["busy@g.us", "quiet@g.us"]


async def test_enable_with_start_fresh_resets_both_windows(db_session: AsyncSession):
    await _seed(db_session)

    await set_managed(db_session, "quiet@g.us", enabled=True, start_fresh=True)

    quiet = await _row(db_session, "quiet@g.us")
    assert quiet.managed is True
    assert quiet.last_ingest > WINDOW and quiet.last_summary_sync > WINDOW


async def test_enable_without_start_fresh_keeps_windows(db_session: AsyncSession):
    await _seed(db_session)

    await set_managed(db_session, "quiet@g.us", enabled=True, start_fresh=False)

    quiet = await _row(db_session, "quiet@g.us")
    assert quiet.managed is True
    assert quiet.last_ingest == WINDOW


async def test_disable_never_touches_windows(db_session: AsyncSession):
    await _seed(db_session)

    await set_managed(db_session, "busy@g.us", enabled=False, start_fresh=True)

    busy = await _row(db_session, "busy@g.us")
    assert busy.managed is False
    assert busy.last_ingest == WINDOW


async def test_spam_needs_owner_only_to_turn_on(db_session: AsyncSession):
    await _seed(db_session)

    with pytest.raises(SpamNeedsOwnerError):
        await set_notify_on_spam(db_session, "quiet@g.us", enabled=True)
    await set_notify_on_spam(db_session, "quiet@g.us", enabled=False)
    await set_notify_on_spam(db_session, "busy@g.us", enabled=True)

    assert (await _row(db_session, "busy@g.us")).notify_on_spam is True


async def test_community_keys_round_trip(db_session: AsyncSession):
    await _seed(db_session)

    await set_community_keys(db_session, "busy@g.us", ["genai", "founders"])
    assert (await _row(db_session, "busy@g.us")).community_keys == ["genai", "founders"]

    await set_community_keys(db_session, "busy@g.us", None)
    assert (await _row(db_session, "busy@g.us")).community_keys is None


async def test_unknown_group_raises(db_session: AsyncSession):
    with pytest.raises(GroupNotFoundError):
        await set_managed(db_session, "nope@g.us", enabled=True, start_fresh=True)


async def test_community_index_lists_only_groups_with_keys(db_session: AsyncSession):
    await _seed(db_session)
    await set_community_keys(db_session, "busy@g.us", ["genai"])

    index = await fetch_community_index(db_session)

    assert [(e.group_jid, e.display_name, e.keys) for e in index] == [
        ("busy@g.us", "Busy", ["genai"])
    ]
