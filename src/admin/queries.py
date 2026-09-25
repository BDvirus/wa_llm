"""Database reads and writes behind the admin page."""

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import col, select, text
from sqlmodel.ext.asyncio.session import AsyncSession

from models import Group


class GroupNotFoundError(LookupError):
    pass


class SpamNeedsOwnerError(ValueError):
    """Enabling spam alerts needs an owner to tag; the handler asserts on it."""


@dataclass(frozen=True)
class GroupRow:
    group_jid: str
    group_name: str | None
    group_topic: str | None
    owner_jid: str | None
    managed: bool
    notify_on_spam: bool
    community_keys: list[str] | None
    last_ingest: datetime
    last_summary_sync: datetime
    total: int
    last_activity: datetime | None
    pending_ingest: int
    pending_summary: int
    topics: int

    @property
    def display_name(self) -> str:
        return self.group_name or self.group_jid


@dataclass(frozen=True)
class CommunityEntry:
    """A group that has community keys - the input for 'shares a key with'."""

    group_jid: str
    display_name: str
    keys: list[str]


# One pass over messages, no N+1. Topics are counted in a separate subquery so
# they don't multiply against the message join.
#
# Timezone assumption: message.timestamp is timestamptz while the window columns
# are naive timestamps written with Python's datetime.now(). Postgres casts the
# naive value using the session TimeZone, so these counts match the pipeline's
# own queries when the app process and Postgres share a timezone - UTC in both
# containers. Running the app on a host in another timezone skews the counts by
# the UTC offset; that affects display only.
_GROUP_ROWS_SQL = """
    SELECT g.group_jid, g.group_name, g.group_topic, g.owner_jid,
           g.managed, g.notify_on_spam, g.community_keys,
           g.last_ingest, g.last_summary_sync,
           count(m.message_id) AS total,
           max(m.timestamp) AS last_activity,
           count(m.message_id) FILTER (
               WHERE m.timestamp >= g.last_ingest AND m.sender_jid <> :bot
           ) AS pending_ingest,
           count(m.message_id) FILTER (
               WHERE m.timestamp >= g.last_summary_sync AND m.sender_jid <> :bot
           ) AS pending_summary,
           coalesce(t.topics, 0) AS topics
    FROM "group" g
    LEFT JOIN message m ON m.group_jid = g.group_jid
    LEFT JOIN (
        SELECT group_jid, count(*) AS topics FROM kbtopic GROUP BY group_jid
    ) t ON t.group_jid = g.group_jid
    {where}
    GROUP BY g.group_jid, t.topics
    ORDER BY g.managed DESC, max(m.timestamp) DESC NULLS LAST, g.group_name
"""


async def fetch_group_rows(session: AsyncSession, bot_jid: str) -> list[GroupRow]:
    """All groups with their stats.

    Pass bot_jid="" when the bot's JID is unknown (gowa down). Never None:
    `sender_jid <> NULL` is NULL and would zero every pending count, hiding
    exactly the backlog the enable dialog exists to warn about.
    """
    result = await session.execute(
        text(_GROUP_ROWS_SQL.format(where="")), {"bot": bot_jid}
    )
    return [GroupRow(**row._mapping) for row in result]


async def fetch_group_row(
    session: AsyncSession, group_jid: str, bot_jid: str
) -> GroupRow | None:
    # The filter is appended rather than written as `:jid IS NULL OR ...`,
    # which asyncpg can't type when the parameter is NULL.
    result = await session.execute(
        text(_GROUP_ROWS_SQL.format(where="WHERE g.group_jid = :jid")),
        {"bot": bot_jid, "jid": group_jid},
    )
    row = result.first()
    return GroupRow(**row._mapping) if row is not None else None


async def fetch_community_index(session: AsyncSession) -> list[CommunityEntry]:
    result = await session.exec(
        select(Group.group_jid, Group.group_name, Group.community_keys).where(
            col(Group.community_keys).is_not(None)
        )
    )
    return [
        CommunityEntry(group_jid=jid, display_name=name or jid, keys=list(keys or []))
        for jid, name, keys in result.all()
    ]


async def _get_group(session: AsyncSession, group_jid: str) -> Group:
    group = await session.get(Group, group_jid)
    if group is None:
        raise GroupNotFoundError(group_jid)
    return group


async def set_managed(
    session: AsyncSession, group_jid: str, *, enabled: bool, start_fresh: bool
) -> None:
    group = await _get_group(session, group_jid)
    group.managed = enabled
    if enabled and start_fresh:
        # Python's naive datetime.now(), like the rest of the codebase. SQL
        # now() would be cast through the Postgres session timezone instead.
        now = datetime.now()
        group.last_ingest = now
        group.last_summary_sync = now
    session.add(group)
    await session.commit()


async def set_notify_on_spam(
    session: AsyncSession, group_jid: str, *, enabled: bool
) -> None:
    group = await _get_group(session, group_jid)
    # Only turning it ON needs an owner. Turning it off must always work:
    # group sync can clear owner_jid while the flag is already on.
    if enabled and not group.owner_jid:
        raise SpamNeedsOwnerError(group_jid)
    group.notify_on_spam = enabled
    session.add(group)
    await session.commit()


async def set_community_keys(
    session: AsyncSession, group_jid: str, keys: list[str] | None
) -> None:
    group = await _get_group(session, group_jid)
    group.community_keys = keys
    session.add(group)
    await session.commit()
