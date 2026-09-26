"""Knowledge-base topics behind the admin page: browse, edit, delete."""

from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from sqlalchemy import func, or_
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from models import KBTopic, Message, Reaction
from models.kb_topic_message import KBTopicMessage
from utils.names import display_names
from utils.redact import UNKNOWN_PERSON
from utils.voyage_embed_text import topic_document
from whatsapp.jid import JIDParseError, parse_jid

PAGE_SIZE = 50
MAX_SOURCE_MESSAGES = 200

Embed = Callable[[str], Awaitable[list[float]]]


class TopicNotFoundError(LookupError):
    pass


class EmptyTopicError(ValueError):
    """Subject and summary are both required - an empty topic can't be searched."""


class EmbeddingFailedError(RuntimeError):
    """Voyage could not embed the new text; nothing was written."""


@dataclass(frozen=True)
class TopicRow:
    id: str
    subject: str
    summary: str
    start_time: datetime
    speakers: list[str]
    messages: int


@dataclass(frozen=True)
class TopicPage:
    topics: list[TopicRow]
    total: int
    page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // PAGE_SIZE))


@dataclass(frozen=True)
class SourceMessage:
    timestamp: datetime
    sender: str
    text: str


@dataclass(frozen=True)
class PurgePreview:
    messages: int
    # Other topics built from some of the same messages: their summaries may
    # still carry what the purge removes.
    shared_with: list[str]


def _user(jid: str) -> str:
    try:
        return parse_jid(jid).user
    except JIDParseError:
        return ""


async def _names_for(session: AsyncSession, jids: set[str]) -> dict[str, str]:
    """Map full sender JIDs to push names, or a neutral label - never a number."""
    names = await display_names(session, {_user(j) for j in jids})
    return {j: names.get(_user(j), UNKNOWN_PERSON) for j in jids}


def _speaker_jids(speakers: str) -> list[str]:
    return [s for s in (speakers or "").split(",") if s]


def _like(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _message_count():
    return (
        select(func.count())
        .where(col(KBTopicMessage.kb_topic_id) == col(KBTopic.id))
        .correlate(KBTopic)
        .scalar_subquery()
    )


async def _rows(
    session: AsyncSession, found: list[tuple[KBTopic, int]]
) -> list[TopicRow]:
    jids = {j for topic, _ in found for j in _speaker_jids(topic.speakers)}
    names = await _names_for(session, jids)
    return [
        TopicRow(
            id=topic.id,
            subject=topic.subject,
            summary=topic.summary,
            start_time=topic.start_time,
            speakers=[names[j] for j in _speaker_jids(topic.speakers)],
            messages=count,
        )
        for topic, count in found
    ]


async def count_topics(session: AsyncSession, group_jid: str) -> int:
    return (
        await session.exec(
            select(func.count()).where(col(KBTopic.group_jid) == group_jid)
        )
    ).one()


async def fetch_topics(
    session: AsyncSession, group_jid: str, *, query: str = "", page: int = 1
) -> TopicPage:
    """One page of a group's topics, newest first, optionally filtered by text."""
    page = max(page, 1)
    where = [col(KBTopic.group_jid) == group_jid]
    if query.strip():
        pattern = _like(query.strip())
        where.append(
            or_(
                col(KBTopic.subject).ilike(pattern, escape="\\"),
                col(KBTopic.summary).ilike(pattern, escape="\\"),
            )
        )

    total = (await session.exec(select(func.count()).where(*where))).one()
    result = await session.exec(
        select(KBTopic, _message_count())
        .where(*where)
        .order_by(col(KBTopic.start_time).desc(), col(KBTopic.id))
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    return TopicPage(
        topics=await _rows(session, list(result.all())), total=total, page=page
    )


async def _get_topic(session: AsyncSession, group_jid: str, topic_id: str) -> KBTopic:
    topic = await session.get(KBTopic, topic_id)
    # A topic from another group is "not found": ids come from the URL.
    if topic is None or topic.group_jid != group_jid:
        raise TopicNotFoundError(topic_id)
    return topic


async def fetch_topic(session: AsyncSession, group_jid: str, topic_id: str) -> TopicRow:
    topic = await _get_topic(session, group_jid, topic_id)
    count = (
        await session.exec(
            select(func.count()).where(col(KBTopicMessage.kb_topic_id) == topic_id)
        )
    ).one()
    return (await _rows(session, [(topic, count)]))[0]


async def fetch_source_messages(
    session: AsyncSession, group_jid: str, topic_id: str
) -> list[SourceMessage]:
    await _get_topic(session, group_jid, topic_id)
    result = await session.exec(
        select(Message.timestamp, Message.sender_jid, Message.text)
        .join(KBTopicMessage, col(KBTopicMessage.message_id) == col(Message.message_id))
        .where(
            col(KBTopicMessage.kb_topic_id) == topic_id,
            col(Message.group_jid) == group_jid,
        )
        .order_by(col(Message.timestamp))
        .limit(MAX_SOURCE_MESSAGES)
    )
    rows = result.all()
    names = await _names_for(session, {sender for _, sender, _ in rows})
    return [
        SourceMessage(timestamp=ts, sender=names[sender], text=text or "")
        for ts, sender, text in rows
    ]


async def update_topic(
    session: AsyncSession,
    group_jid: str,
    topic_id: str,
    *,
    subject: str,
    summary: str,
    embed: Embed,
) -> None:
    """Save new text together with a fresh embedding.

    The embedding is computed first: if Voyage fails nothing is written, so
    the stored text never drifts from the vector search runs against.
    """
    subject, summary = subject.strip(), summary.strip()
    if not subject or not summary:
        raise EmptyTopicError(topic_id)
    topic = await _get_topic(session, group_jid, topic_id)
    try:
        embedding = await embed(topic_document(subject, summary))
    except Exception as exc:
        raise EmbeddingFailedError(topic_id) from exc
    topic.subject = subject
    topic.summary = summary
    topic.embedding = embedding
    session.add(topic)
    await session.commit()


async def _source_ids(
    session: AsyncSession, group_jid: str, topic_id: str
) -> list[str]:
    result = await session.exec(
        select(KBTopicMessage.message_id)
        .join(Message, col(Message.message_id) == col(KBTopicMessage.message_id))
        .where(
            col(KBTopicMessage.kb_topic_id) == topic_id,
            col(Message.group_jid) == group_jid,
        )
    )
    return list(result.all())


async def purge_preview(
    session: AsyncSession, group_jid: str, topic_id: str
) -> PurgePreview:
    await _get_topic(session, group_jid, topic_id)
    ids = await _source_ids(session, group_jid, topic_id)
    shared = await session.exec(
        select(KBTopic.subject)
        .join(KBTopicMessage, col(KBTopicMessage.kb_topic_id) == col(KBTopic.id))
        .where(
            col(KBTopicMessage.message_id).in_(ids),
            col(KBTopic.id) != topic_id,
        )
        .distinct()
        .order_by(col(KBTopic.subject))
    )
    return PurgePreview(messages=len(ids), shared_with=list(shared.all()))


async def delete_topic(
    session: AsyncSession, group_jid: str, topic_id: str, *, with_messages: bool
) -> int:
    """Delete a topic; optionally its source messages too. Returns messages deleted.

    Deleting messages also removes their reactions and their links to other
    topics (both reference message). The other topics themselves stay.
    """
    await _get_topic(session, group_jid, topic_id)
    ids = await _source_ids(session, group_jid, topic_id) if with_messages else []

    await session.exec(
        delete(KBTopicMessage).where(col(KBTopicMessage.kb_topic_id) == topic_id)
    )
    if ids:
        await session.exec(
            delete(KBTopicMessage).where(col(KBTopicMessage.message_id).in_(ids))
        )
        await session.exec(delete(Reaction).where(col(Reaction.message_id).in_(ids)))
        await session.exec(
            delete(Message).where(
                col(Message.message_id).in_(ids), col(Message.group_jid) == group_jid
            )
        )
    await session.exec(delete(KBTopic).where(col(KBTopic.id) == topic_id))
    await session.commit()
    return len(ids)
