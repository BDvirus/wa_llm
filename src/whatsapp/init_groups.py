import logging
from datetime import datetime

from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession

from models import Group, BaseGroup, Sender, BaseSender, upsert
from .client import WhatsAppClient
from .members import sync_members

logger = logging.getLogger(__name__)

# Arbitrary constant key for pg_advisory_xact_lock: group syncs run at startup
# and on every group.* webhook, which arrive in bursts and are retried by gowa.
_GROUP_SYNC_LOCK_KEY = 0x77614C4C6D


async def _bot_identity(client: WhatsAppClient) -> str | None:
    try:
        return (await client.get_my_jid()).normalize_str()
    except Exception:
        logger.warning(
            "Could not resolve the bot's JID during group sync", exc_info=True
        )
        return None


async def gather_groups(session: AsyncSession, client: WhatsAppClient) -> None:
    groups = await client.get_user_groups()

    if groups is None or groups.results is None:
        return

    # One sync at a time; released when the surrounding transaction ends.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _GROUP_SYNC_LOCK_KEY}
    )
    bot_identity = await _bot_identity(client)

    for g in groups.results.data:
        if not g.jid:
            continue
        owner_usr = g.owner_pn or g.owner_jid or None
        if owner_usr and (await session.get(Sender, owner_usr)) is None:
            owner = Sender(
                **BaseSender(
                    jid=owner_usr,
                ).model_dump()
            )
            await upsert(session, owner)

        existing_group = await session.get(Group, g.jid)

        # upsert() writes every column, so each admin-controlled column must be
        # carried over here - a missing one silently resets to its default.
        group = Group(
            **BaseGroup(
                group_jid=g.jid,
                group_name=g.name,
                group_topic=g.topic,
                owner_jid=owner_usr,
                managed=existing_group.managed if existing_group else False,
                community_keys=existing_group.community_keys
                if existing_group
                else None,
                last_ingest=existing_group.last_ingest
                if existing_group
                else datetime.now(),
                last_summary_sync=existing_group.last_summary_sync
                if existing_group
                else datetime.now(),
                notify_on_spam=existing_group.notify_on_spam
                if existing_group
                else False,
                dm_queries_enabled=existing_group.dm_queries_enabled
                if existing_group
                else False,
                created_at=existing_group.created_at
                if existing_group
                else datetime.now(),
            ).model_dump()
        )
        await upsert(session, group)
        await sync_members(session, g.jid, g.participants, bot_identity)
