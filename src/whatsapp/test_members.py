"""Membership sync and gather_groups, against real Postgres."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gowa_sdk.models import Group as GowaGroup
from gowa_sdk.models import Participant
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from models import Group, GroupMember
from whatsapp.init_groups import gather_groups
from whatsapp.jid import JID
from whatsapp.members import participant_identities, sync_members

BOT = "972500000000@s.whatsapp.net"
G = "120363@g.us"


def participant(**fields) -> Participant:
    return Participant.model_validate(fields)


class TestParticipantIdentities:
    def test_collects_jid_lid_and_phone_under_one_key(self):
        p = participant(
            JID="111:3@lid", LID="111@lid", PhoneNumber="972501111111@s.whatsapp.net"
        )
        key, identities = participant_identities(p)
        assert identities == {"111@lid", "972501111111@s.whatsapp.net"}
        assert key == "972501111111@s.whatsapp.net"  # phone form preferred as the key

    def test_lid_only_participant_uses_lid_as_key(self):
        key, identities = participant_identities(participant(JID="222@lid"))
        assert (key, identities) == ("222@lid", {"222@lid"})

    def test_empty_participant_has_no_identities(self):
        assert participant_identities(participant()) == (None, set())


async def _members(session: AsyncSession) -> set[tuple[str, str]]:
    rows = await session.exec(select(GroupMember.identity, GroupMember.participant))
    return set(rows.all())


@pytest.mark.integration
async def test_sync_inserts_every_identity_and_skips_the_bot(db_session: AsyncSession):
    db_session.add(Group(group_jid=G))
    await db_session.commit()

    await sync_members(
        db_session,
        G,
        [
            participant(JID="972501111111@s.whatsapp.net", LID="111@lid"),
            participant(JID=BOT, LID="999@lid"),
        ],
        bot_identity=BOT,
    )
    await db_session.commit()

    assert await _members(db_session) == {
        ("972501111111@s.whatsapp.net", "972501111111@s.whatsapp.net"),
        ("111@lid", "972501111111@s.whatsapp.net"),
    }


@pytest.mark.integration
async def test_sync_removes_only_departed_members(db_session: AsyncSession):
    db_session.add(Group(group_jid=G))
    await db_session.commit()
    stays = participant(JID="972501111111@s.whatsapp.net")
    leaves = participant(JID="972502222222@s.whatsapp.net")

    await sync_members(db_session, G, [stays, leaves], bot_identity=BOT)
    await sync_members(db_session, G, [stays], bot_identity=BOT)
    await db_session.commit()

    assert {i for i, _ in await _members(db_session)} == {"972501111111@s.whatsapp.net"}


@pytest.mark.integration
@pytest.mark.parametrize("missing", [None, []])
async def test_missing_participants_never_wipe_membership(
    db_session: AsyncSession, missing
):
    db_session.add(Group(group_jid=G))
    await db_session.commit()
    await sync_members(
        db_session,
        G,
        [participant(JID="972501111111@s.whatsapp.net")],
        bot_identity=BOT,
    )

    await sync_members(db_session, G, missing, bot_identity=BOT)
    await db_session.commit()

    assert len(await _members(db_session)) == 1


@pytest.mark.integration
async def test_unidentifiable_participants_never_wipe_membership(
    db_session: AsyncSession,
):
    db_session.add(Group(group_jid=G))
    await db_session.commit()
    await sync_members(
        db_session, G, [participant(JID="972501111111@s.whatsapp.net")], BOT
    )

    await sync_members(db_session, G, [participant(), participant()], BOT)
    await db_session.commit()

    assert len(await _members(db_session)) == 1


def _client(groups: list[GowaGroup]) -> AsyncMock:
    client = AsyncMock()
    client.get_user_groups = AsyncMock(
        return_value=SimpleNamespace(results=SimpleNamespace(data=groups))
    )
    client.get_my_jid = AsyncMock(
        return_value=JID(user="972500000000", server="s.whatsapp.net")
    )
    return client


@pytest.mark.integration
async def test_gather_groups_keeps_admin_settings_and_syncs_members(
    db_session: AsyncSession,
):
    # Regression: gather_groups rebuilds each group from an explicit field list
    # and upserts every column, so an unlisted column silently reset to False.
    db_session.add(
        Group(group_jid=G, managed=True, notify_on_spam=True, dm_queries_enabled=True)
    )
    await db_session.commit()

    gowa_group = GowaGroup.model_validate(
        {
            "JID": G,
            "Name": "Renamed",
            "Participants": [{"JID": "972501111111@s.whatsapp.net"}],
        }
    )
    await gather_groups(db_session, _client([gowa_group]))
    await db_session.commit()

    db_session.expire_all()
    group = await db_session.get(Group, G)
    assert group is not None
    assert (
        group.group_name,
        group.managed,
        group.notify_on_spam,
        group.dm_queries_enabled,
    ) == (
        "Renamed",
        True,
        True,
        True,
    )
    assert {i for i, _ in await _members(db_session)} == {"972501111111@s.whatsapp.net"}
