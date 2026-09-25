"""Authorization, quota and catch-up queries against real Postgres."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from models import Group, GroupMember, Message, Sender
from private_chat.access import allowed_groups, fetch_catch_up, requests_last_24h

pytestmark = pytest.mark.integration

USER = "972501111111@s.whatsapp.net"
USER_LID = "111@lid"
OTHER = "972502222222@s.whatsapp.net"
BOT = "972500000000@s.whatsapp.net"
NOW = datetime.now(timezone.utc)


async def _seed_groups(session: AsyncSession) -> None:
    session.add_all(
        [
            Group(
                group_jid="open@g.us",
                group_name="Open",
                managed=True,
                dm_queries_enabled=True,
            ),
            Group(group_jid="toggle-off@g.us", managed=True, dm_queries_enabled=False),
            Group(group_jid="unmanaged@g.us", managed=False, dm_queries_enabled=True),
            Group(group_jid="not-mine@g.us", managed=True, dm_queries_enabled=True),
        ]
    )
    await session.flush()
    for g in ("open@g.us", "toggle-off@g.us", "unmanaged@g.us"):
        session.add(GroupMember(identity=USER, group_jid=g, participant=USER))
    session.add(GroupMember(identity=USER_LID, group_jid="open@g.us", participant=USER))
    session.add(
        GroupMember(identity=OTHER, group_jid="not-mine@g.us", participant=OTHER)
    )
    await session.commit()


class TestAllowedGroups:
    async def test_only_managed_enabled_groups_the_user_is_in(self, db_session):
        await _seed_groups(db_session)
        groups = await allowed_groups(db_session, {USER})
        assert [g.group_jid for g in groups] == ["open@g.us"]

    async def test_matches_by_lid(self, db_session):
        await _seed_groups(db_session)
        assert [g.group_jid for g in await allowed_groups(db_session, {USER_LID})] == [
            "open@g.us"
        ]

    async def test_stranger_gets_nothing(self, db_session):
        await _seed_groups(db_session)
        assert await allowed_groups(db_session, {"972509999999@s.whatsapp.net"}) == []

    async def test_no_identities_gets_nothing(self, db_session):
        await _seed_groups(db_session)
        assert await allowed_groups(db_session, set()) == []


async def _dm(session, message_id, sender, minutes_ago, chat=USER, group=None):
    session.add(
        Message(
            message_id=message_id,
            chat_jid=chat,
            group_jid=group,
            sender_jid=sender,
            text="hi",
            timestamp=NOW - timedelta(minutes=minutes_ago),
        )
    )


class TestQuota:
    async def test_counts_only_this_users_private_messages_in_24h(self, db_session):
        db_session.add_all(
            [
                Sender(jid=USER),
                Sender(jid=BOT),
                Sender(jid=OTHER),
                Group(group_jid="g@g.us"),
            ]
        )
        await db_session.flush()
        await _dm(db_session, "1", USER, 5)
        await _dm(db_session, "2", USER, 60 * 23)
        await _dm(db_session, "old", USER, 60 * 25)  # outside the window
        await _dm(db_session, "bot", BOT, 4)  # the bot's reply
        await _dm(
            db_session, "grp", USER, 3, chat="g@g.us", group="g@g.us"
        )  # a group message
        await _dm(db_session, "else", OTHER, 2, chat=OTHER)  # someone else's chat
        await db_session.commit()

        assert await requests_last_24h(db_session, chat_jid=USER, sender_jid=USER) == 2


class TestCatchUp:
    async def test_per_group_window_excludes_bot_and_truncates(self, db_session):
        db_session.add_all(
            [
                Sender(jid=USER),
                Sender(jid=BOT),
                Group(group_jid="a@g.us"),
                Group(group_jid="b@g.us"),
            ]
        )
        await db_session.flush()
        db_session.add_all(
            [
                Message(
                    message_id="a-old",
                    chat_jid="a@g.us",
                    group_jid="a@g.us",
                    sender_jid=USER,
                    text="old",
                    timestamp=NOW - timedelta(hours=30),
                ),
                Message(
                    message_id="a-new",
                    chat_jid="a@g.us",
                    group_jid="a@g.us",
                    sender_jid=USER,
                    text="x" * 1000,
                    timestamp=NOW - timedelta(hours=1),
                ),
                Message(
                    message_id="a-bot",
                    chat_jid="a@g.us",
                    group_jid="a@g.us",
                    sender_jid=BOT,
                    text="bot",
                    timestamp=NOW - timedelta(minutes=30),
                ),
                Message(
                    message_id="b-new",
                    chat_jid="b@g.us",
                    group_jid="b@g.us",
                    sender_jid=USER,
                    text="b",
                    timestamp=NOW - timedelta(hours=2),
                ),
            ]
        )
        await db_session.commit()

        result = await fetch_catch_up(
            db_session,
            ["a@g.us", "b@g.us"],
            since=NOW - timedelta(hours=24),
            bot_identity=BOT,
        )

        assert {g: [m.message_id for m in msgs] for g, msgs in result.items()} == {
            "a@g.us": ["a-new"],
            "b@g.us": ["b-new"],
        }
        assert len(result["a@g.us"][0].text) <= 401  # truncated

    async def test_total_is_capped_keeping_the_newest(self, db_session):
        db_session.add_all([Sender(jid=USER), Group(group_jid="a@g.us")])
        await db_session.flush()
        for i in range(120):
            db_session.add(
                Message(
                    message_id=f"m{i}",
                    chat_jid="a@g.us",
                    group_jid="a@g.us",
                    sender_jid=USER,
                    text=str(i),
                    timestamp=NOW - timedelta(minutes=i + 1),
                )
            )
        await db_session.commit()

        result = await fetch_catch_up(
            db_session,
            ["a@g.us"],
            since=NOW - timedelta(days=1),
            bot_identity=None,
            per_group=100,
            total=50,
        )

        ids = [m.message_id for m in result["a@g.us"]]
        assert len(ids) == 50
        assert ids[-1] == "m0"  # chronological order, newest last
