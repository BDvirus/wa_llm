import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from admin.jobs import (
    JobKind,
    JobOutcome,
    JobRegistry,
    JobState,
    ingest_runner,
    summarize_runner,
)
from load_new_kbtopics import IngestResult
from models import Group

JID = "123@g.us"


async def _finish(registry: JobRegistry, kind: JobKind = JobKind.INGEST) -> None:
    await asyncio.wait_for(registry.wait(JID, kind), timeout=2)


class TestJobRegistry:
    @pytest.mark.asyncio
    async def test_runs_to_done_and_records_message(self):
        registry = JobRegistry()

        async def runner(group_jid: str) -> JobOutcome:
            return JobOutcome(JobState.DONE, f"ok {group_jid}")

        started = registry.start(JID, JobKind.INGEST, runner)
        assert started.state is JobState.RUNNING

        await _finish(registry)
        status = registry.get(JID, JobKind.INGEST)
        assert status is not None
        assert status.state is JobState.DONE
        assert status.message == f"ok {JID}"
        assert status.finished_at is not None

    @pytest.mark.asyncio
    async def test_duplicate_start_while_running_does_not_rerun(self):
        registry = JobRegistry()
        release = asyncio.Event()
        calls = 0

        async def runner(_: str) -> JobOutcome:
            nonlocal calls
            calls += 1
            await release.wait()
            return JobOutcome(JobState.DONE, "")

        first = registry.start(JID, JobKind.INGEST, runner)
        second = registry.start(JID, JobKind.INGEST, runner)
        release.set()
        await _finish(registry)

        assert second is first
        assert calls == 1

    @pytest.mark.asyncio
    async def test_different_kinds_run_independently(self):
        registry = JobRegistry()

        async def runner(_: str) -> JobOutcome:
            return JobOutcome(JobState.DONE, "")

        registry.start(JID, JobKind.INGEST, runner)
        registry.start(JID, JobKind.SUMMARIZE, runner)
        await _finish(registry, JobKind.INGEST)
        await _finish(registry, JobKind.SUMMARIZE)

        assert registry.get(JID, JobKind.SUMMARIZE).state is JobState.DONE  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_exception_becomes_error_state(self):
        registry = JobRegistry()

        async def runner(_: str) -> JobOutcome:
            raise RuntimeError("postgresql://user:secret@db/internal")

        registry.start(JID, JobKind.INGEST, runner)
        await _finish(registry)

        status = registry.get(JID, JobKind.INGEST)
        assert status is not None
        assert status.state is JobState.ERROR
        assert "RuntimeError" in status.message
        # Exception text stays in the log, never in the UI.
        assert "secret" not in status.message

    @pytest.mark.asyncio
    async def test_can_restart_after_finish(self):
        registry = JobRegistry()
        calls = 0

        async def runner(_: str) -> JobOutcome:
            nonlocal calls
            calls += 1
            return JobOutcome(JobState.DONE, "")

        registry.start(JID, JobKind.INGEST, runner)
        await _finish(registry)
        registry.start(JID, JobKind.INGEST, runner)
        await _finish(registry)

        assert calls == 2

    @pytest.mark.asyncio
    async def test_shutdown_cancels_running_jobs(self):
        registry = JobRegistry()

        async def runner(_: str) -> JobOutcome:
            await asyncio.sleep(60)
            return JobOutcome(JobState.DONE, "")

        registry.start(JID, JobKind.INGEST, runner)
        await asyncio.sleep(0)
        await registry.shutdown(timeout=2)

        status = registry.get(JID, JobKind.INGEST)
        assert status is not None
        assert status.state is JobState.ERROR

    @pytest.mark.asyncio
    async def test_get_unknown_job_returns_none(self):
        assert JobRegistry().get(JID, JobKind.INGEST) is None


def _app_state(group: Group | None) -> tuple[SimpleNamespace, MagicMock]:
    session = MagicMock()
    session.get = AsyncMock(return_value=group)

    @asynccontextmanager
    async def async_session():
        yield session

    state = SimpleNamespace(
        async_session=async_session,
        settings=MagicMock(),
        whatsapp=AsyncMock(),
        embedding_client=AsyncMock(),
    )
    return state, session


class TestIngestRunner:
    @pytest.mark.asyncio
    async def test_refetches_group_inside_its_own_session(self):
        group = Group(group_jid=JID)
        state, session = _app_state(group)

        with patch(
            "admin.jobs.topicsLoader.load_topics",
            AsyncMock(return_value=IngestResult(messages=40, chunks=2)),
        ) as load:
            outcome = await ingest_runner(state)(JID)

        session.get.assert_awaited_once_with(Group, JID)
        assert load.await_args.args[0] is session  # type: ignore[union-attr]
        assert outcome.state is JobState.DONE
        assert "40" in outcome.message and "2" in outcome.message

    @pytest.mark.asyncio
    async def test_missing_group_is_an_error(self):
        state, _ = _app_state(None)
        outcome = await ingest_runner(state)(JID)
        assert outcome.state is JobState.ERROR

    @pytest.mark.asyncio
    async def test_no_new_messages_is_skipped(self):
        state, _ = _app_state(Group(group_jid=JID))
        with patch(
            "admin.jobs.topicsLoader.load_topics",
            AsyncMock(return_value=IngestResult(messages=0, chunks=0)),
        ):
            outcome = await ingest_runner(state)(JID)
        assert outcome.state is JobState.SKIPPED


class TestSummarizeRunner:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("result", "expected_state"),
        [
            ("sent", JobState.DONE),
            ("too_few_messages", JobState.SKIPPED),
            ("summarize_failed", JobState.ERROR),
            ("send_failed", JobState.ERROR),
        ],
    )
    async def test_maps_every_outcome(self, result, expected_state):
        state, session = _app_state(Group(group_jid=JID))
        with patch(
            "admin.jobs.summarize_and_send_to_group", AsyncMock(return_value=result)
        ):
            outcome = await summarize_runner(state)(JID)

        assert outcome.state is expected_state
        session.get.assert_awaited_once_with(Group, JID)

    @pytest.mark.asyncio
    async def test_send_failed_explains_the_lost_summary(self):
        state, _ = _app_state(Group(group_jid=JID))
        with patch(
            "admin.jobs.summarize_and_send_to_group",
            AsyncMock(return_value="send_failed"),
        ):
            outcome = await summarize_runner(state)(JID)
        assert "אבד" in outcome.message
