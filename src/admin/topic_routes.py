"""Admin routes for one group's knowledge base: browse, edit, delete topics."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from voyageai.client_async import AsyncClient

from api.deps import get_text_embebedding
from models import Group
from utils.voyage_embed_text import voyage_embed_text

from .router import HtmxOnly, Session, templates
from .topics import (
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/groups/{group_jid}/topics", include_in_schema=False)

Embedding = Annotated[AsyncClient, Depends(get_text_embebedding)]

GROUP_NOT_FOUND = "הקבוצה לא נמצאה"
TOPIC_NOT_FOUND = "הנושא לא נמצא — ייתכן שכבר נמחק"


def _not_found(text: str) -> Response:
    return PlainTextResponse(text, status_code=404)


async def _topic_view(
    request: Request, session: Session, group_jid: str, topic_id: str
) -> Response:
    try:
        topic = await fetch_topic(session, group_jid, topic_id)
    except TopicNotFoundError:
        return _not_found(TOPIC_NOT_FOUND)
    return templates.TemplateResponse(
        request, "_topic.html", {"topic": topic, "group_jid": group_jid}
    )


@router.get("", response_class=HTMLResponse)
async def topics_page(
    request: Request, group_jid: str, session: Session, q: str = "", page: int = 1
):
    group = await session.get(Group, group_jid)
    if group is None:
        return _not_found(GROUP_NOT_FOUND)
    result = await fetch_topics(session, group_jid, query=q, page=page)
    return templates.TemplateResponse(
        request,
        "topics.html",
        {
            "group": group,
            "group_jid": group_jid,
            "result": result,
            "q": q,
            "total": await count_topics(session, group_jid),
        },
    )


@router.get("/{topic_id}", response_class=HTMLResponse)
async def topic_view(request: Request, group_jid: str, topic_id: str, session: Session):
    return await _topic_view(request, session, group_jid, topic_id)


@router.get("/{topic_id}/edit", response_class=HTMLResponse)
async def topic_edit(request: Request, group_jid: str, topic_id: str, session: Session):
    try:
        topic = await fetch_topic(session, group_jid, topic_id)
    except TopicNotFoundError:
        return _not_found(TOPIC_NOT_FOUND)
    return templates.TemplateResponse(
        request, "_topic_edit.html", {"topic": topic, "group_jid": group_jid}
    )


@router.post("/{topic_id}", dependencies=HtmxOnly, response_class=HTMLResponse)
async def topic_save(
    request: Request,
    group_jid: str,
    topic_id: str,
    session: Session,
    embedding_client: Embedding,
    subject: Annotated[str, Form()] = "",
    summary: Annotated[str, Form()] = "",
):
    async def embed(document: str) -> list[float]:
        return (await voyage_embed_text(embedding_client, [document]))[0]

    try:
        await update_topic(
            session, group_jid, topic_id, subject=subject, summary=summary, embed=embed
        )
    except TopicNotFoundError:
        return _not_found(TOPIC_NOT_FOUND)
    except EmptyTopicError:
        return PlainTextResponse("כותרת וסיכום הם שדות חובה", status_code=422)
    except EmbeddingFailedError:
        logger.exception("Re-embedding topic %s failed", topic_id)
        return PlainTextResponse(
            "חישוב ה-embedding נכשל (Voyage), ולכן הנושא לא נשמר. נסו שוב.",
            status_code=502,
        )
    return await _topic_view(request, session, group_jid, topic_id)


@router.get("/{topic_id}/messages", response_class=HTMLResponse)
async def topic_messages(
    request: Request, group_jid: str, topic_id: str, session: Session
):
    try:
        messages = await fetch_source_messages(session, group_jid, topic_id)
    except TopicNotFoundError:
        return _not_found(TOPIC_NOT_FOUND)
    return templates.TemplateResponse(
        request, "_topic_sources.html", {"messages": messages}
    )


@router.get("/{topic_id}/purge", response_class=HTMLResponse)
async def purge_dialog(
    request: Request, group_jid: str, topic_id: str, session: Session
):
    try:
        topic = await fetch_topic(session, group_jid, topic_id)
        preview = await purge_preview(session, group_jid, topic_id)
    except TopicNotFoundError:
        return _not_found(TOPIC_NOT_FOUND)
    return templates.TemplateResponse(
        request,
        "_confirm_purge.html",
        {"topic": topic, "preview": preview, "group_jid": group_jid},
    )


@router.post("/{topic_id}/delete", dependencies=HtmxOnly, response_class=HTMLResponse)
async def topic_delete(
    group_jid: str,
    topic_id: str,
    session: Session,
    with_messages: Annotated[bool, Form()] = False,
):
    try:
        await delete_topic(session, group_jid, topic_id, with_messages=with_messages)
    except TopicNotFoundError:
        return _not_found(TOPIC_NOT_FOUND)
    # The empty main body removes the topic's article; the out-of-band span
    # keeps the header count in step.
    total = await count_topics(session, group_jid)
    return HTMLResponse(
        f'<span id="topic-total" class="num" hx-swap-oob="true">{total:,}</span>'
    )
