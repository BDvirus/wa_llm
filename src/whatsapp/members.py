"""Group membership sync: gowa participants -> group_member rows."""

import logging
from typing import Iterable

from gowa_sdk.models import Participant
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import col, delete
from sqlmodel.ext.asyncio.session import AsyncSession

from models import GroupMember

from .jid import DefaultUserServer, normalize_identity

logger = logging.getLogger(__name__)


def participant_identities(participant: Participant) -> tuple[str | None, set[str]]:
    """All identities one participant can appear as, plus one stable key.

    In LID-addressed groups `JID` is the @lid and the phone number arrives in
    `PhoneNumber`, which the SDK model doesn't declare but keeps via
    extra="allow". The phone form is preferred as the key when present.
    """
    raw = (
        participant.jid,
        participant.lid,
        (participant.model_extra or {}).get("PhoneNumber"),
    )
    identities = {i for i in (normalize_identity(r) for r in raw) if i}
    if not identities:
        return None, set()
    ordered = sorted(identities)
    key = next((i for i in ordered if i.endswith(f"@{DefaultUserServer}")), ordered[0])
    return key, identities


async def sync_members(
    session: AsyncSession,
    group_jid: str,
    participants: Iterable[Participant] | None,
    bot_identity: str | None,
) -> None:
    """Make group_member for one group match gowa's participant list.

    Syncs by difference (delete departed identities, upsert current ones)
    rather than delete-all-then-insert, so overlapping syncs can't collide on
    the primary key. A missing or empty list leaves membership untouched: a
    partial gowa response must never revoke (or corrupt) access.
    """
    participants = list(participants or [])
    if not participants:
        logger.warning(
            "No participants for %s; leaving membership unchanged", group_jid
        )
        return

    rows: dict[str, str] = {}
    resolved = False
    for participant in participants:
        key, identities = participant_identities(participant)
        if key is None:
            continue
        resolved = True
        # The bot is in every managed group; it must never look "authorized".
        if bot_identity and bot_identity in identities:
            continue
        for identity in identities:
            rows[identity] = key

    # Nothing parseable is a malformed response, not an empty group: without
    # this, NOT IN () below would delete every member.
    if not resolved:
        logger.warning(
            "No identifiable participants for %s; leaving membership unchanged",
            group_jid,
        )
        return

    await session.exec(
        delete(GroupMember).where(
            col(GroupMember.group_jid) == group_jid,
            col(GroupMember.identity).not_in(list(rows)),
        )
    )
    if rows:
        stmt = insert(GroupMember).values(
            [
                {"identity": identity, "group_jid": group_jid, "participant": key}
                for identity, key in rows.items()
            ]
        )
        await session.exec(
            stmt.on_conflict_do_update(
                index_elements=["identity", "group_jid"],
                set_={"participant": stmt.excluded.participant},
            )
        )
