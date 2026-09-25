"""Text shown to the user in a private chat. Pure functions."""

from dataclasses import dataclass
from datetime import datetime

from utils.relative_time import format_relative

SNIPPET_LENGTH = 200


@dataclass(frozen=True)
class SearchHit:
    group_name: str
    sender_name: str
    timestamp: datetime
    text: str


def format_search_hits(
    hits: list[SearchHit], query: str, now: datetime | None = None
) -> str:
    if not hits:
        return f"לא נמצאו הודעות עבור «{query}» בקבוצות שלך."
    lines = [f"🔎 תוצאות עבור «{query}»:"]
    for hit in hits:
        snippet = (
            hit.text
            if len(hit.text) <= SNIPPET_LENGTH
            else hit.text[:SNIPPET_LENGTH] + "…"
        )
        lines.append(
            f"\n*{hit.group_name}* · {hit.sender_name} · {format_relative(hit.timestamp, now)}\n{snippet}"
        )
    return "\n".join(lines)


def help_text(group_names: list[str], daily_quota: int) -> str:
    groups = "\n".join(f"• {name}" for name in group_names)
    return (
        "היי! אפשר לשאול אותי בפרטי על הידע של הקבוצות שלך:\n"
        f"{groups}\n\n"
        "*שאלה* — פשוט כתבו אותה, ואענה מתוך מה שנדון בקבוצות.\n"
        "*חפש <מילים>* — חיפוש הודעות, למשל: חפש קורס פייתון\n"
        "*מה פספסתי* — סיכום אישי של מה שקרה מאז הפעם הקודמת\n"
        "*עזרה* — ההודעה הזו\n\n"
        f"עד {daily_quota} בקשות ביממה."
    )
