from datetime import datetime, timezone
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin import router as admin_router_module
from admin.jobs import JobKind, JobOutcome, JobRegistry, JobState
from admin.queries import GroupRow, PrivateChatNeedsManagedError, SpamNeedsOwnerError
from api.deps import get_db_async_session, get_whatsapp
from models import Group
from whatsapp.jid import JID

HX = {"HX-Request": "true"}
JID_STR = "120363@g.us"


def make_row(**overrides) -> GroupRow:
    values = dict(
        group_jid=JID_STR,
        group_name="Test Group",
        group_topic=None,
        owner_jid="111@s.whatsapp.net",
        managed=False,
        notify_on_spam=False,
        dm_queries_enabled=False,
        community_keys=None,
        last_ingest=datetime(2026, 1, 1),
        last_summary_sync=datetime(2026, 1, 1),
        total=40,
        last_activity=datetime(2026, 9, 1, tzinfo=timezone.utc),
        pending_ingest=30,
        pending_summary=30,
        topics=3,
        members=12,
    )
    return GroupRow(**{**values, **overrides})


@pytest.fixture
def whatsapp() -> AsyncMock:
    client = AsyncMock()
    client.get_my_jid = AsyncMock(return_value=JID(user="999", server="s.whatsapp.net"))
    return client


@pytest.fixture
def session() -> MagicMock:
    s = MagicMock()
    s.get = AsyncMock(return_value=Group(group_jid=JID_STR, group_name="Test Group"))
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    return s


@pytest.fixture
def client(session, whatsapp) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(admin_router_module.router)
    app.state.jobs = JobRegistry()

    async def override_session():
        yield session

    app.dependency_overrides[get_db_async_session] = override_session
    app.dependency_overrides[get_whatsapp] = lambda: whatsapp
    with TestClient(app) as test_client:
        yield test_client
        # Cancel anything still running on the app's loop before it closes.
        test_client.portal.call(app.state.jobs.shutdown)  # type: ignore[union-attr]


@pytest.fixture
def queries():
    """Patch the query layer so router tests need no database."""
    with (
        patch.object(
            admin_router_module,
            "fetch_group_rows",
            AsyncMock(return_value=[make_row()]),
        ) as rows,
        patch.object(
            admin_router_module, "fetch_group_row", AsyncMock(return_value=make_row())
        ) as row,
        patch.object(
            admin_router_module, "fetch_community_index", AsyncMock(return_value=[])
        ),
        patch.object(admin_router_module, "set_managed", AsyncMock()) as managed,
        patch.object(admin_router_module, "set_notify_on_spam", AsyncMock()) as spam,
        patch.object(admin_router_module, "set_community_keys", AsyncMock()) as keys,
        patch.object(admin_router_module, "set_dm_queries", AsyncMock()) as dm,
    ):
        yield MagicMock(
            rows=rows, row=row, managed=managed, spam=spam, keys=keys, dm=dm
        )


class TestPage:
    def test_renders_groups(self, client, queries):
        response = client.get("/admin")
        assert response.status_code == 200
        assert "Test Group" in response.text
        queries.rows.assert_awaited_once()
        assert queries.rows.await_args.args[1] == "999@s.whatsapp.net"

    def test_escapes_user_controlled_group_names(self, client, queries):
        queries.rows.return_value = [make_row(group_name="<script>alert(1)</script>")]
        response = client.get("/admin")
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;" in response.text

    def test_escapes_names_inside_hx_confirm(self, client, queries):
        queries.rows.return_value = [make_row(group_name='x" hx-on:click="alert(1)')]
        response = client.get("/admin")
        assert 'hx-on:click="alert(1)' not in response.text

    def test_renders_when_gowa_is_down(self, client, queries, whatsapp):
        whatsapp.get_my_jid.side_effect = RuntimeError("gowa down")
        response = client.get("/admin")
        assert response.status_code == 200
        assert "gowa לא זמין" in response.text
        # "" not None: a NULL comparison would zero every pending count.
        assert queries.rows.await_args.args[1] == ""

    def test_unnamed_group_gets_placeholder_not_a_bidi_mangled_jid(
        self, client, queries
    ):
        queries.rows.return_value = [make_row(group_name=None)]
        text = client.get("/admin").text
        assert "ללא שם" in text
        assert f'<span class="jid" dir="ltr">{JID_STR}</span>' in text

    def test_empty_state(self, client, queries):
        queries.rows.return_value = []
        assert "אין עדיין קבוצות" in client.get("/admin").text


class TestCsrfGuard:
    @pytest.mark.parametrize(
        "path",
        [
            f"/admin/groups/{JID_STR}/managed",
            f"/admin/groups/{JID_STR}/spam",
            f"/admin/groups/{JID_STR}/community-keys",
            f"/admin/groups/{JID_STR}/jobs/ingest",
        ],
    )
    def test_posts_without_hx_request_are_forbidden(self, client, queries, path):
        assert client.post(path).status_code == 403


class TestManaged:
    def test_missing_checkboxes_mean_false(self, client, queries):
        response = client.post(f"/admin/groups/{JID_STR}/managed", headers=HX)
        assert response.status_code == 200
        queries.managed.assert_awaited_once()
        assert queries.managed.await_args.kwargs == {
            "enabled": False,
            "start_fresh": False,
        }

    def test_enable_with_start_fresh(self, client, queries):
        client.post(
            f"/admin/groups/{JID_STR}/managed",
            data={"enabled": "true", "start_fresh": "true"},
            headers=HX,
        )
        assert queries.managed.await_args.kwargs == {
            "enabled": True,
            "start_fresh": True,
        }

    def test_returns_a_fresh_row(self, client, queries):
        response = client.post(f"/admin/groups/{JID_STR}/managed", headers=HX)
        assert "is-fresh" in response.text
        assert "<tr" in response.text


class TestEnableDialog:
    def test_shows_pending_counts(self, client, queries):
        queries.row.return_value = make_row(pending_summary=42, pending_ingest=17)
        text = client.get(f"/admin/groups/{JID_STR}/enable").text
        assert "42" in text and "17" in text
        assert 'name="start_fresh"' in text and "checked" in text

    def test_warns_about_backlog(self, client, queries):
        queries.row.return_value = make_row(pending_summary=8531)
        assert "callout--amber" in client.get(f"/admin/groups/{JID_STR}/enable").text

    def test_unknown_group_is_404(self, client, queries):
        queries.row.return_value = None
        assert client.get(f"/admin/groups/{JID_STR}/enable").status_code == 404


class TestSpam:
    def test_enabling_without_owner_is_rejected_server_side(self, client, queries):
        queries.spam.side_effect = SpamNeedsOwnerError(JID_STR)
        response = client.post(
            f"/admin/groups/{JID_STR}/spam", data={"enabled": "true"}, headers=HX
        )
        assert response.status_code == 422
        assert "בעלים" in response.text

    def test_disabling_works(self, client, queries):
        response = client.post(f"/admin/groups/{JID_STR}/spam", headers=HX)
        assert response.status_code == 200
        assert queries.spam.await_args.kwargs == {"enabled": False}

    def test_switch_is_disabled_without_owner(self, client, queries):
        queries.rows.return_value = [make_row(owner_jid=None)]
        assert "אין בעלים לתייג" in client.get("/admin").text


class TestCommunityKeys:
    def test_parses_before_saving(self, client, queries):
        client.post(
            f"/admin/groups/{JID_STR}/community-keys",
            data={"keys": " genai, founders ,genai"},
            headers=HX,
        )
        assert queries.keys.await_args.args[2] == ["genai", "founders"]


class TestJobs:
    def test_start_then_poll_until_done(self, client, queries):
        async def runner(_: str) -> JobOutcome:
            return JobOutcome(JobState.DONE, "עובדו 3 הודעות")

        with patch.dict(
            admin_router_module.RUNNERS, {JobKind.INGEST: lambda _state: runner}
        ):
            started = client.post(f"/admin/groups/{JID_STR}/jobs/ingest", headers=HX)
            assert started.status_code == 200
            assert "בניית ידע" in started.text

            # The job finishes on the app's loop; the next poll shows it done.
            polled = client.get(f"/admin/groups/{JID_STR}/jobs/ingest").text
            assert "עובדו 3 הודעות" in polled
            assert 'hx-trigger="every 2s"' not in polled  # polling stops

    def test_running_job_keeps_polling(self, client, queries):
        with patch.dict(
            admin_router_module.RUNNERS,
            {JobKind.SUMMARIZE: lambda _state: _never_finishes},
        ):
            client.post(f"/admin/groups/{JID_STR}/jobs/summarize", headers=HX)
            text = client.get(f"/admin/groups/{JID_STR}/jobs/summarize").text
        assert 'hx-trigger="every 2s"' in text
        assert "disabled" in text  # no double start while running

    def test_summarize_button_confirms(self, client, queries):
        text = client.get(f"/admin/groups/{JID_STR}/jobs/summarize").text
        assert "hx-confirm" in text

    def test_unknown_kind_is_rejected(self, client, queries):
        assert (
            client.post(f"/admin/groups/{JID_STR}/jobs/delete", headers=HX).status_code
            == 422
        )

    def test_unknown_group_is_404(self, client, queries, session):
        session.get.return_value = None
        assert (
            client.post(f"/admin/groups/{JID_STR}/jobs/ingest", headers=HX).status_code
            == 404
        )

    def test_polling_a_vanished_group_tells_htmx_to_stop(
        self, client, queries, session
    ):
        session.get.return_value = None
        assert client.get(f"/admin/groups/{JID_STR}/jobs/ingest").status_code == 286


async def _never_finishes(_: str) -> JobOutcome:
    import asyncio

    await asyncio.sleep(3600)
    return JobOutcome(JobState.DONE, "")


class TestPrivateChatToggle:
    def test_enable(self, client, queries):
        response = client.post(
            f"/admin/groups/{JID_STR}/private-chat",
            data={"enabled": "true"},
            headers=HX,
        )
        assert response.status_code == 200
        assert queries.dm.await_args.kwargs == {"enabled": True}

    def test_enabling_an_unmanaged_group_is_rejected_server_side(self, client, queries):
        queries.dm.side_effect = PrivateChatNeedsManagedError(JID_STR)
        response = client.post(
            f"/admin/groups/{JID_STR}/private-chat",
            data={"enabled": "true"},
            headers=HX,
        )
        assert response.status_code == 422

    def test_switch_is_disabled_for_unmanaged_groups(self, client, queries):
        queries.rows.return_value = [make_row(managed=False)]
        assert "רק בקבוצה מנוהלת" in client.get("/admin").text

    def test_requires_hx_request(self, client, queries):
        assert client.post(f"/admin/groups/{JID_STR}/private-chat").status_code == 403

    def test_member_count_and_unsynced_hint(self, client, queries):
        assert "12 חברים" in client.get("/admin").text
        queries.rows.return_value = [make_row(members=0)]
        assert "חברים לא סונכרנו" in client.get("/admin").text
