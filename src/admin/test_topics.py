"""KB topic management against real Postgres."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from admin.topics import (
    PAGE_SIZE,
    EmbeddingFailedError,
    EmptyTopicError,
    TopicNotFoundError,
    count_topics,
    delete_topic,
    fetch_source_messages,
    fetch_topic,
    fetch_topics,
    purge_preview,
    update_topic,
)
from models import Group, KBTopic, Message, Reaction, Sender
from models.kb_topic_message import KBTopicMessage

pytestmark = pytest.mark.integration

G = "a@g.us"
OTHER_G = "b@g.us"
DANA = "972501111111@s.whatsapp.net"
NOW = datetime.now(timezone.utc)
VEC = [0.0] * 1024


def _topic(id: str, group: str = G, minutes_ago: int = 0, **kw) -> KBTopic:
    return KBTopic(
        id=id,
        group_jid=group,
        start_time=NOW - timedelta(minutes=minutes_ago),
        speakers=kw.pop("speakers", DANA),
        subject=kw.pop("subject", f"subject {id}"),
        summary=kw.pop("summary", f"summary {id}"),
        embedding=VEC,
    )


def _message(id: str, group: str = G) -> Message:
    return Message(
        message_id=id,
        chat_jid=group,
        group_jid=group,
        sender_jid=DANA,
        text=f"text {id}",
        timestamp=NOW,
    )


async def _seed(session: AsyncSession) -> None:
    """t1 <- m1, m2 ; t2 <- m2 (shared) ; m1 has a reaction ; x in another group."""
    session.add_all(
        [
            Sender(jid=DANA, push_name="Dana"),
            Group(group_jid=G, group_name="A"),
            Group(group_jid=OTHER_G, group_name="B"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            _message("m1"),
            _message("m2"),
            _message("m3"),
            _message("x1", OTHER_G),
            _topic("t1", subject="Meetup plans"),
            _topic("t2", minutes_ago=5, subject="Budget"),
            _topic("x", OTHER_G),
        ]
    )
    await session.flush()
    session.add_all(
        [
            KBTopicMessage(kb_topic_id="t1", message_id="m1"),
            KBTopicMessage(kb_topic_id="t1", message_id="m2"),
            KBTopicMessage(kb_topic_id="t2", message_id="m2"),
            KBTopicMessage(kb_topic_id="x", message_id="x1"),
            Reaction(message_id="m1", sender_jid=DANA, emoji="👍"),
        ]
    )
    await session.commit()


async def _ids(session: AsyncSession, model, column) -> set[str]:
    return set((await session.exec(select(column))).all())


class TestBrowse:
    async def test_lists_group_topics_newest_first_with_names(self, db_session):
        await _seed(db_session)
        page = await fetch_topics(db_session, G)
        assert [t.id for t in page.topics] == ["t1", "t2"]
        assert page.total == 2
        assert page.topics[0].messages == 2
        assert page.topics[0].speakers == ["Dana"]
        assert await count_topics(db_session, G) == 2

    async def test_filter_matches_subject_or_summary_and_escapes_wildcards(
        self, db_session
    ):
        await _seed(db_session)
        assert [
            t.id for t in (await fetch_topics(db_session, G, query="meetup")).topics
        ] == ["t1"]
        assert [
            t.id for t in (await fetch_topics(db_session, G, query="summary t2")).topics
        ] == ["t2"]
        assert (await fetch_topics(db_session, G, query="%")).total == 0

    async def test_pagination(self, db_session):
        db_session.add(Group(group_jid=G))
        await db_session.flush()
        db_session.add_all(
            [_topic(f"p{i}", minutes_ago=i) for i in range(PAGE_SIZE + 3)]
        )
        await db_session.commit()

        second = await fetch_topics(db_session, G, page=2)
        assert [t.id for t in second.topics] == [
            f"p{i}" for i in range(PAGE_SIZE, PAGE_SIZE + 3)
        ]
        assert second.pages == 2

    async def test_source_messages_are_named_and_scoped(self, db_session):
        await _seed(db_session)
        messages = await fetch_source_messages(db_session, G, "t1")
        assert [m.text for m in messages] == ["text m1", "text m2"]
        assert {m.sender for m in messages} == {"Dana"}

    async def test_topic_of_another_group_is_not_found(self, db_session):
        await _seed(db_session)
        for call in (
            fetch_topic(db_session, G, "x"),
            fetch_source_messages(db_session, G, "x"),
            purge_preview(db_session, G, "x"),
            delete_topic(db_session, G, "x", with_messages=True),
        ):
            with pytest.raises(TopicNotFoundError):
                await call
        assert "x" in await _ids(db_session, KBTopic, KBTopic.id)


class TestEdit:
    async def test_saves_text_with_a_fresh_embedding(self, db_session):
        await _seed(db_session)
        embed = AsyncMock(return_value=[1.0] * 1024)

        await update_topic(
            db_session, G, "t1", subject=" New ", summary="Better", embed=embed
        )

        embed.assert_awaited_once_with("# New\nBetter")
        topic = await db_session.get(KBTopic, "t1")
        assert topic is not None
        assert (topic.subject, topic.summary) == ("New", "Better")
        assert list(topic.embedding)[0] == 1.0

    async def test_embedding_failure_writes_nothing(self, db_session):
        await _seed(db_session)
        embed = AsyncMock(side_effect=RuntimeError("voyage down"))

        with pytest.raises(EmbeddingFailedError):
            await update_topic(
                db_session, G, "t1", subject="New", summary="x", embed=embed
            )
        await db_session.rollback()

        topic = await db_session.get(KBTopic, "t1")
        assert topic is not None and topic.subject == "Meetup plans"

    async def test_empty_text_is_rejected_before_embedding(self, db_session):
        await _seed(db_session)
        embed = AsyncMock()
        with pytest.raises(EmptyTopicError):
            await update_topic(
                db_session, G, "t1", subject="  ", summary="x", embed=embed
            )
        embed.assert_not_awaited()


class TestDelete:
    async def test_topic_only_keeps_messages(self, db_session):
        await _seed(db_session)

        assert await delete_topic(db_session, G, "t1", with_messages=False) == 0

        assert await _ids(db_session, KBTopic, KBTopic.id) == {"t2", "x"}
        assert {"m1", "m2", "m3"} <= await _ids(db_session, Message, Message.message_id)
        assert await _ids(db_session, KBTopicMessage, KBTopicMessage.kb_topic_id) == {
            "t2",
            "x",
        }

    async def test_preview_counts_messages_and_names_shared_topics(self, db_session):
        await _seed(db_session)
        preview = await purge_preview(db_session, G, "t1")
        assert preview.messages == 2
        assert preview.shared_with == ["Budget"]

    async def test_purge_removes_messages_reactions_and_shared_links(self, db_session):
        await _seed(db_session)

        assert await delete_topic(db_session, G, "t1", with_messages=True) == 2

        assert await _ids(db_session, Message, Message.message_id) == {"m3", "x1"}
        assert await _ids(db_session, Reaction, Reaction.message_id) == set()
        # t2 survives, but its link to the purged m2 is gone.
        assert await _ids(db_session, KBTopic, KBTopic.id) == {"t2", "x"}
        assert await _ids(db_session, KBTopicMessage, KBTopicMessage.kb_topic_id) == {
            "x"
        }
