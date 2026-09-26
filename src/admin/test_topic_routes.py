from datetime import datetime, timezone
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin import topic_routes
from admin.topics import (
    EmbeddingFailedError,
    EmptyTopicError,
    PurgePreview,
    SourceMessage,
    TopicNotFoundError,
    TopicPage,
    TopicRow,
)
from api.deps import get_db_async_session, get_text_embebedding
from models import Group

HX = {"HX-Request": "true"}
G = "120363@g.us"
BASE = f"/admin/groups/{G}/topics"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def make_topic(**overrides) -> TopicRow:
    values = dict(
        id="abc123",
        subject="Meetup plans",
        summary="We meet on Sunday",
        start_time=NOW,
        speakers=["Dana"],
        messages=2,
    )
    return TopicRow(**{**values, **overrides})


@pytest.fixture
def session() -> MagicMock:
    s = MagicMock()
    s.get = AsyncMock(return_value=Group(group_jid=G, group_name="Founders"))
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    return s


@pytest.fixture
def client(session) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(topic_routes.router)

    async def override_session():
        yield session

    app.dependency_overrides[get_db_async_session] = override_session
    app.dependency_overrides[get_text_embebedding] = lambda: AsyncMock()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def total():
    with patch.object(topic_routes, "count_topics", AsyncMock(return_value=41)) as t:
        yield t


def _patch(name: str, **kw):
    return patch.object(topic_routes, name, AsyncMock(**kw))


class TestPage:
    def test_lists_topics_and_escapes_their_text(self, client):
        page = TopicPage(
            topics=[make_topic(subject="<script>x</script>")], total=1, page=1
        )
        with _patch("fetch_topics", return_value=page) as fetch:
            response = client.get(BASE, params={"q": "meet", "page": 2})

        assert response.status_code == 200
        assert "Founders" in response.text
        assert '<span id="topic-total" class="num">41</span>' in response.text
        assert "<script>x</script>" not in response.text
        assert "&lt;script&gt;" in response.text
        assert fetch.await_args is not None
        assert fetch.await_args.kwargs == {"query": "meet", "page": 2}

    def test_unknown_group_is_404(self, client, session):
        session.get.return_value = None
        assert client.get(BASE).status_code == 404

    def test_empty_state(self, client):
        with _patch("fetch_topics", return_value=TopicPage(topics=[], total=0, page=1)):
            assert "אין עדיין נושאים" in client.get(BASE).text


class TestEdit:
    def test_save_re_embeds_and_returns_the_view(self, client):
        with (
            _patch("update_topic") as update,
            _patch("fetch_topic", return_value=make_topic(subject="New")),
            _patch("voyage_embed_text", return_value=[[0.5] * 1024]),
        ):
            response = client.post(
                f"{BASE}/abc123",
                data={"subject": "New", "summary": "Better"},
                headers=HX,
            )

        assert response.status_code == 200
        assert 'id="t-abc123"' in response.text and "New" in response.text
        assert update.await_args is not None
        assert update.await_args.kwargs["subject"] == "New"
        assert update.await_args.kwargs["summary"] == "Better"

    def test_embed_callable_uses_voyage(self, client):
        async def run_embed(*args, **kwargs):
            assert await kwargs["embed"]("doc") == [0.5] * 1024

        with (
            patch.object(topic_routes, "update_topic", side_effect=run_embed),
            _patch("fetch_topic", return_value=make_topic()),
            _patch("voyage_embed_text", return_value=[[0.5] * 1024]) as voyage,
        ):
            client.post(
                f"{BASE}/abc123", data={"subject": "a", "summary": "b"}, headers=HX
            )
        assert voyage.await_args is not None
        assert voyage.await_args.args[1] == ["doc"]

    def test_voyage_failure_is_reported_and_nothing_saved(self, client):
        with _patch("update_topic", side_effect=EmbeddingFailedError("abc123")):
            response = client.post(
                f"{BASE}/abc123", data={"subject": "a", "summary": "b"}, headers=HX
            )
        assert response.status_code == 502
        assert "לא נשמר" in response.text

    def test_other_failures_are_not_blamed_on_voyage(self, client):
        # Must propagate so the session dependency rolls back.
        with _patch("update_topic", side_effect=RuntimeError("db down")):
            with pytest.raises(RuntimeError):
                client.post(
                    f"{BASE}/abc123", data={"subject": "a", "summary": "b"}, headers=HX
                )

    def test_empty_fields_are_422(self, client):
        with _patch("update_topic", side_effect=EmptyTopicError("abc123")):
            response = client.post(f"{BASE}/abc123", data={}, headers=HX)
        assert response.status_code == 422

    def test_edit_form_renders_current_text(self, client):
        with _patch("fetch_topic", return_value=make_topic()):
            response = client.get(f"{BASE}/abc123/edit")
        assert 'name="subject" value="Meetup plans"' in response.text
        assert "We meet on Sunday</textarea>" in response.text


class TestDelete:
    def test_state_changes_require_htmx(self, client):
        with _patch("delete_topic") as delete, _patch("update_topic") as update:
            assert client.post(f"{BASE}/abc123/delete").status_code == 403
            assert client.post(f"{BASE}/abc123", data={}).status_code == 403
        delete.assert_not_awaited()
        update.assert_not_awaited()

    @pytest.mark.parametrize(
        ("data", "expected"), [({}, False), ({"with_messages": "true"}, True)]
    )
    def test_delete_mode_is_passed_through(self, client, data, expected):
        with _patch("delete_topic", return_value=0) as delete:
            response = client.post(f"{BASE}/abc123/delete", data=data, headers=HX)
        assert response.status_code == 200
        # Nothing but the out-of-band header count: the article is swapped away.
        assert response.text == (
            '<span id="topic-total" class="num" hx-swap-oob="true">41</span>'
        )
        assert delete.await_args is not None
        assert delete.await_args.kwargs == {"with_messages": expected}

    def test_missing_topic_is_404(self, client):
        with _patch("delete_topic", side_effect=TopicNotFoundError("abc123")):
            response = client.post(f"{BASE}/abc123/delete", headers=HX)
        assert response.status_code == 404

    def test_purge_dialog_shows_count_and_shared_topics(self, client):
        with (
            _patch("fetch_topic", return_value=make_topic()),
            _patch(
                "purge_preview",
                return_value=PurgePreview(messages=7, shared_with=["Budget"]),
            ),
        ):
            response = client.get(f"{BASE}/abc123/purge")
        assert response.status_code == 200
        assert ">7<" in response.text and "Budget" in response.text
        assert 'name="with_messages" value="true"' in response.text


class TestSources:
    def test_lists_named_messages(self, client):
        messages = [SourceMessage(timestamp=NOW, sender="Dana", text="hello")]
        with _patch("fetch_source_messages", return_value=messages):
            response = client.get(f"{BASE}/abc123/messages")
        assert "Dana" in response.text and "hello" in response.text
