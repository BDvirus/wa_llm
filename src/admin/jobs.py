"""Per-group background jobs (ingest / summarize) for the admin page.

Jobs run as asyncio tasks in the web process and report into an in-memory
registry that the page polls. State is lost on restart, which is fine for a
single-process deployment.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

from load_new_kbtopics import topicsLoader
from models import Group
from summarize_and_send_to_groups import (
    MIN_MESSAGES_TO_SUMMARIZE,
    SummaryOutcome,
    summarize_and_send_to_group,
)

logger = logging.getLogger(__name__)


class JobKind(str, Enum):
    INGEST = "ingest"
    SUMMARIZE = "summarize"


class JobState(str, Enum):
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class JobOutcome:
    """What a runner reports when it finishes."""

    state: JobState
    message: str


@dataclass(frozen=True)
class JobStatus:
    kind: JobKind
    state: JobState
    started_at: datetime
    finished_at: datetime | None = None
    message: str = ""


Runner = Callable[[str], Awaitable[JobOutcome]]
_Key = tuple[str, JobKind]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobRegistry:
    def __init__(self) -> None:
        self._statuses: dict[_Key, JobStatus] = {}
        # Holding the task reference matters: asyncio only keeps weak references
        # to running tasks, so an unreferenced one can be garbage-collected.
        self._tasks: dict[_Key, asyncio.Task[None]] = {}

    def get(self, group_jid: str, kind: JobKind) -> JobStatus | None:
        return self._statuses.get((group_jid, kind))

    def start(self, group_jid: str, kind: JobKind, runner: Runner) -> JobStatus:
        """Start a job, or return the one already running for this group and kind."""
        key = (group_jid, kind)
        current = self._statuses.get(key)
        if current is not None and current.state is JobState.RUNNING:
            return current

        status = JobStatus(kind=kind, state=JobState.RUNNING, started_at=_now())
        self._statuses[key] = status
        task = asyncio.create_task(self._run(key, runner, status))
        self._tasks[key] = task
        task.add_done_callback(lambda done: self._forget(key, done))
        return status

    async def wait(self, group_jid: str, kind: JobKind) -> None:
        """Wait until the job for this group and kind (if any) finishes."""
        task = self._tasks.get((group_jid, kind))
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Cancel running jobs so they don't outlive the database engine."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                # Best effort: a task stuck in a call that doesn't yield may
                # still touch the engine as it's disposed. Make that visible.
                logger.warning(
                    "%d admin job(s) still running after %.0fs shutdown timeout",
                    len(pending),
                    timeout,
                )

    def _forget(self, key: _Key, done: asyncio.Task[None]) -> None:
        # Only drop the reference if it still points at this task; a new run
        # for the same key may already have been registered.
        if self._tasks.get(key) is done:
            del self._tasks[key]

    async def _run(self, key: _Key, runner: Runner, started: JobStatus) -> None:
        try:
            outcome = await runner(key[0])
            final = replace(started, state=outcome.state, message=outcome.message)
        except asyncio.CancelledError:
            self._statuses[key] = replace(
                started,
                state=JobState.ERROR,
                message="העבודה בוטלה בכיבוי השרת",
                finished_at=_now(),
            )
            raise
        except Exception as exc:
            logger.exception("Admin job %s failed for group %s", key[1].value, key[0])
            # Only the exception type reaches the UI; the message can carry
            # internals (connection strings, payload fragments) - it's in the log.
            final = replace(
                started,
                state=JobState.ERROR,
                message=f"שגיאה פנימית ({type(exc).__name__}) — הפרטים בלוג",
            )
        self._statuses[key] = replace(final, finished_at=_now())


_SUMMARY_OUTCOMES: dict[SummaryOutcome, JobOutcome] = {
    "sent": JobOutcome(JobState.DONE, "הסיכום נשלח לקבוצה ולקבוצות הקהילה"),
    "too_few_messages": JobOutcome(
        JobState.SKIPPED,
        f"פחות מ-{MIN_MESSAGES_TO_SUMMARIZE} הודעות חדשות — לא נשלח סיכום",
    ),
    "summarize_failed": JobOutcome(
        JobState.ERROR, "יצירת הסיכום נכשלה. חלון הסיכום לא השתנה, אפשר לנסות שוב"
    ),
    "send_failed": JobOutcome(
        JobState.ERROR, "השליחה נכשלה, אבל חלון הסיכום כבר קודם — הסיכום הזה אבד"
    ),
}


def ingest_runner(state: Any) -> Runner:
    """Build an ingest runner from app.state (session factory, clients)."""

    async def run(group_jid: str) -> JobOutcome:
        # Own session, own Group instance: the request's session is gone by
        # the time this runs, and the ingest code calls session.add(group).
        async with state.async_session() as session:
            group = await session.get(Group, group_jid)
            if group is None:
                return JobOutcome(JobState.ERROR, "הקבוצה לא נמצאה")
            result = await topicsLoader().load_topics(
                session, group, state.embedding_client, state.whatsapp
            )
        if result.messages == 0:
            return JobOutcome(
                JobState.SKIPPED, "אין הודעות חדשות מאז בניית הידע האחרונה"
            )
        return JobOutcome(
            JobState.DONE,
            f"עובדו {result.messages} הודעות ב-{result.chunks} קטעים",
        )

    return run


def summarize_runner(state: Any) -> Runner:
    """Build a summarize runner from app.state (session factory, clients)."""

    async def run(group_jid: str) -> JobOutcome:
        async with state.async_session() as session:
            group = await session.get(Group, group_jid)
            if group is None:
                return JobOutcome(JobState.ERROR, "הקבוצה לא נמצאה")
            outcome = await summarize_and_send_to_group(
                state.settings, session, state.whatsapp, group
            )
        return _SUMMARY_OUTCOMES[outcome]

    return run


RUNNERS: dict[JobKind, Callable[[Any], Runner]] = {
    JobKind.INGEST: ingest_runner,
    JobKind.SUMMARIZE: summarize_runner,
}
