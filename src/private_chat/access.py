"""Who may use the private chat, how much, and what they may read."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import col, desc, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from models import Group, GroupMember, Message

QUOTA_WINDOW = timedelta(hours=24)
CATCH_UP_PER_GROUP = 100
CATCH_UP_TOTAL = 300
CATCH_UP_MESSAGE_CHARS = 400


async def allowed_groups(session: AsyncSession, identities: set[str]) -> list[Group]:
    """Managed groups with private questions enabled that the user is a member of.

    `identities` are the user's normalized JIDs/LIDs. No identities, no groups.
    """
    if not identities:
        return []
    member_groups = select(GroupMember.group_jid).where(
        col(GroupMember.identity).in_(sorted(identities))
    )
    result = await session.exec(
        select(Group)
        .where(Group.managed == True)  # noqa: E712
        .where(Group.dm_queries_enabled == True)  # noqa: E712
        .where(col(Group.group_jid).in_(member_groups))
        .order_by(Group.group_name)
    )
    return list(result.all())


async def requests_last_24h(
    session: AsyncSession, *, chat_jid: str, sender_jid: str
) -> int:
    """How many private messages this user sent in this chat in the last 24 hours.

    Counted from the message table (every private message is stored), so the
    current message - already stored - is included. The bot's replies and
    group messages are excluded.
    """
    since = datetime.now(timezone.utc) - QUOTA_WINDOW
    result = await session.exec(
        select(func.count())
        .select_from(Message)
        .where(Message.chat_jid == chat_jid)
        .where(Message.sender_jid == sender_jid)
        .where(col(Message.group_jid).is_(None))
        .where(col(Message.timestamp) >= since)
    )
    return int(result.one())


@dataclass(frozen=True)
class CatchUpMessage:
    message_id: str
    timestamp: datetime
    sender_jid: str
    text: str


async def fetch_catch_up(
    session: AsyncSession,
    group_jids: list[str],
    *,
    since: datetime,
    bot_identity: str | None,
    per_group: int = CATCH_UP_PER_GROUP,
    total: int = CATCH_UP_TOTAL,
) -> dict[str, list[CatchUpMessage]]:
    """Recent messages per group since `since`, bounded for one LLM call.

    Up to `per_group` newest messages per group and `total` overall (newest
    kept), each cut to CATCH_UP_MESSAGE_CHARS - unlike the summary pipeline,
    whose queries have no LIMIT. Returned in chronological order per group.
    """
    collected: list[tuple[str, CatchUpMessage]] = []
    for group_jid in group_jids:
        stmt = (
            select(Message)
            .where(Message.group_jid == group_jid)
            .where(col(Message.timestamp) >= since)
            .where(col(Message.text).is_not(None))
            .order_by(desc(Message.timestamp))
            .limit(per_group)
        )
        if bot_identity:
            stmt = stmt.where(Message.sender_jid != bot_identity)
        for msg in (await session.exec(stmt)).all():
            text = msg.text or ""
            if len(text) > CATCH_UP_MESSAGE_CHARS:
                text = text[:CATCH_UP_MESSAGE_CHARS] + "…"
            collected.append(
                (
                    group_jid,
                    CatchUpMessage(msg.message_id, msg.timestamp, msg.sender_jid, text),
                )
            )

    newest = sorted(collected, key=lambda item: item[1].timestamp, reverse=True)[:total]
    by_group: dict[str, list[CatchUpMessage]] = {}
    for group_jid, msg in sorted(newest, key=lambda item: item[1].timestamp):
        by_group.setdefault(group_jid, []).append(msg)
    return by_group
