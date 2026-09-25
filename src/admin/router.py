"""Admin page routes: a thin layer over queries, jobs and templates."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel.ext.asyncio.session import AsyncSession

from api.deps import get_db_async_session, get_whatsapp
from models import Group
from whatsapp import WhatsAppClient

from .display import (
    BACKLOG_WARNING_THRESHOLD,
    PendingLevel,
    dom_id,
    format_relative,
    meter_fill,
    pending_level,
    related_groups,
)
from .forms import parse_community_keys
from .jobs import RUNNERS, JobKind, JobRegistry, JobState, JobStatus
from .queries import (
    CommunityEntry,
    GroupNotFoundError,
    GroupRow,
    SpamNeedsOwnerError,
    fetch_community_index,
    fetch_group_row,
    fetch_group_rows,
    set_community_keys,
    set_managed,
    set_notify_on_spam,
)

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Starlette's Jinja2Templates uses select_autoescape(), which escapes .html
# templates only - every template here must keep the .html extension, since
# group names come from WhatsApp and are user-controlled.
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.filters["relative"] = format_relative
templates.env.filters["dom_id"] = dom_id

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

HTMX_STOP_POLLING = 286

Session = Annotated[AsyncSession, Depends(get_db_async_session)]
WhatsApp = Annotated[WhatsAppClient, Depends(get_whatsapp)]


def require_htmx(request: Request) -> None:
    """CSRF guard for state-changing requests.

    A cross-site page can't set a custom header without a CORS preflight, and
    the app has no CORS middleware, so the preflight fails. That blocks
    forged POSTs riding on the Authentik session cookie.
    """
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(status_code=403, detail="Admin actions require HX-Request")


def get_jobs(request: Request) -> JobRegistry:
    return request.app.state.jobs


Jobs = Annotated[JobRegistry, Depends(get_jobs)]
HtmxOnly = [Depends(require_htmx)]


@dataclass(frozen=True)
class JobView:
    group_jid: str
    group_name: str
    kind: JobKind
    status: JobStatus | None

    @property
    def running(self) -> bool:
        return self.status is not None and self.status.state is JobState.RUNNING


async def _bot_jid(whatsapp: WhatsAppClient) -> tuple[str, bool]:
    """The bot's JID, or ("", False) when gowa is unreachable.

    "" rather than None keeps the pending counts meaningful - see
    fetch_group_rows.
    """
    try:
        return (await whatsapp.get_my_jid()).normalize_str(), True
    except Exception:
        logger.warning("gowa unreachable while rendering the admin page", exc_info=True)
        return "", False


def _row_item(
    row: GroupRow, index: list[CommunityEntry], jobs: JobRegistry
) -> dict[str, Any]:
    return {
        "row": row,
        "related": related_groups(row.group_jid, row.community_keys, index),
        "level": pending_level(row.pending_summary),
        "fill": meter_fill(row.pending_summary),
        "jobs": [
            JobView(
                row.group_jid, row.display_name, kind, jobs.get(row.group_jid, kind)
            )
            for kind in JobKind
        ],
    }


async def _render_row(
    request: Request,
    session: AsyncSession,
    whatsapp: WhatsAppClient,
    jobs: JobRegistry,
    group_jid: str,
) -> Response:
    bot, _ = await _bot_jid(whatsapp)
    row = await fetch_group_row(session, group_jid, bot)
    if row is None:
        return PlainTextResponse("הקבוצה לא נמצאה", status_code=404)
    index = await fetch_community_index(session)
    # `fresh` marks a just-updated row so it flashes once; full page loads don't.
    return templates.TemplateResponse(
        request, "_row.html", {"item": _row_item(row, index, jobs), "fresh": True}
    )


@router.get("", response_class=HTMLResponse)
async def groups_page(
    request: Request, session: Session, whatsapp: WhatsApp, jobs: Jobs
):
    bot, gowa_ok = await _bot_jid(whatsapp)
    rows = await fetch_group_rows(session, bot)
    index = await fetch_community_index(session)
    items = [_row_item(row, index, jobs) for row in rows]
    running = sum(1 for item in items for job in item["jobs"] if job.running)
    return templates.TemplateResponse(
        request,
        "groups.html",
        {
            "items": items,
            "gowa_ok": gowa_ok,
            "managed_count": sum(1 for row in rows if row.managed),
            "running_count": running,
        },
    )


@router.get("/groups/{group_jid}/enable", response_class=HTMLResponse)
async def enable_dialog(
    request: Request, group_jid: str, session: Session, whatsapp: WhatsApp
):
    bot, gowa_ok = await _bot_jid(whatsapp)
    row = await fetch_group_row(session, group_jid, bot)
    if row is None:
        return PlainTextResponse("הקבוצה לא נמצאה", status_code=404)
    return templates.TemplateResponse(
        request,
        "_confirm_enable.html",
        {
            "row": row,
            "gowa_ok": gowa_ok,
            "backlog": pending_level(row.pending_summary) is PendingLevel.BACKLOG,
            "threshold": BACKLOG_WARNING_THRESHOLD,
        },
    )


@router.post(
    "/groups/{group_jid}/managed", dependencies=HtmxOnly, response_class=HTMLResponse
)
async def update_managed(
    request: Request,
    group_jid: str,
    session: Session,
    whatsapp: WhatsApp,
    jobs: Jobs,
    # Unchecked checkboxes are simply absent from the form - default to False.
    enabled: Annotated[bool, Form()] = False,
    start_fresh: Annotated[bool, Form()] = False,
):
    try:
        await set_managed(session, group_jid, enabled=enabled, start_fresh=start_fresh)
    except GroupNotFoundError:
        return PlainTextResponse("הקבוצה לא נמצאה", status_code=404)
    return await _render_row(request, session, whatsapp, jobs, group_jid)


@router.post(
    "/groups/{group_jid}/spam", dependencies=HtmxOnly, response_class=HTMLResponse
)
async def update_spam(
    request: Request,
    group_jid: str,
    session: Session,
    whatsapp: WhatsApp,
    jobs: Jobs,
    enabled: Annotated[bool, Form()] = False,
):
    try:
        await set_notify_on_spam(session, group_jid, enabled=enabled)
    except GroupNotFoundError:
        return PlainTextResponse("הקבוצה לא נמצאה", status_code=404)
    except SpamNeedsOwnerError:
        # Enforced server-side too: a disabled switch is not a check.
        return PlainTextResponse(
            "אי אפשר להפעיל התראות ספאם בקבוצה בלי בעלים — אין את מי לתייג",
            status_code=422,
        )
    return await _render_row(request, session, whatsapp, jobs, group_jid)


@router.post(
    "/groups/{group_jid}/community-keys",
    dependencies=HtmxOnly,
    response_class=HTMLResponse,
)
async def update_community_keys(
    request: Request,
    group_jid: str,
    session: Session,
    whatsapp: WhatsApp,
    jobs: Jobs,
    keys: Annotated[str, Form()] = "",
):
    try:
        await set_community_keys(session, group_jid, parse_community_keys(keys))
    except GroupNotFoundError:
        return PlainTextResponse("הקבוצה לא נמצאה", status_code=404)
    return await _render_row(request, session, whatsapp, jobs, group_jid)


async def _job_response(
    request: Request,
    session: AsyncSession,
    group_jid: str,
    kind: JobKind,
    status: JobStatus | None,
) -> Response:
    group = await session.get(Group, group_jid)
    if group is None:
        return PlainTextResponse("הקבוצה לא נמצאה", status_code=404)
    job = JobView(group_jid, group.group_name or group_jid, kind, status)
    return templates.TemplateResponse(request, "_job.html", {"job": job})


@router.post(
    "/groups/{group_jid}/jobs/{kind}",
    dependencies=HtmxOnly,
    response_class=HTMLResponse,
)
async def start_job(
    request: Request, group_jid: str, kind: JobKind, session: Session, jobs: Jobs
):
    if await session.get(Group, group_jid) is None:
        return PlainTextResponse("הקבוצה לא נמצאה", status_code=404)
    status = jobs.start(group_jid, kind, RUNNERS[kind](request.app.state))
    return await _job_response(request, session, group_jid, kind, status)


@router.get("/groups/{group_jid}/jobs/{kind}", response_class=HTMLResponse)
async def job_status(
    request: Request, group_jid: str, kind: JobKind, session: Session, jobs: Jobs
):
    if await session.get(Group, group_jid) is None:
        # 286 is htmx's "stop polling" status. A 404 isn't swapped, so the
        # polling element would stay and retry forever.
        return PlainTextResponse("הקבוצה לא נמצאה", status_code=HTMX_STOP_POLLING)
    return await _job_response(
        request, session, group_jid, kind, jobs.get(group_jid, kind)
    )
