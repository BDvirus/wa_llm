from typing import Iterable

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from models import Sender
from utils.redact import phone_users
from whatsapp.jid import DefaultUserServer


async def display_names(session: AsyncSession, users: Iterable[str]) -> dict[str, str]:
    """Map phone user parts (e.g. "972501234567") to known WhatsApp push names.

    Users without a push name - or whose push name is itself a phone number -
    are left out; callers fall back to a neutral label rather than the number.
    """
    jids = sorted({f"{user}@{DefaultUserServer}" for user in users if user})
    if not jids:
        return {}
    rows = await session.exec(
        select(Sender.jid, Sender.push_name).where(col(Sender.jid).in_(jids))
    )
    return {
        jid.split("@", 1)[0]: name
        for jid, name in rows.all()
        if name and not phone_users(name)
    }
