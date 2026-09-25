"""Presentation helpers for the admin page. Pure functions, no I/O."""

import re
from datetime import datetime, timezone
from enum import Enum

from summarize_and_send_to_groups import MIN_MESSAGES_TO_SUMMARIZE

from .queries import CommunityEntry

# Above this many unsummarized messages, the first summary becomes one very
# large LLM call - worth a visible warning before enabling a group.
BACKLOG_WARNING_THRESHOLD = 500

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR
_MONTH = 30 * _DAY


class PendingLevel(str, Enum):
    BELOW_MINIMUM = "below-minimum"  # a summary run would skip this group
    NORMAL = "normal"
    BACKLOG = "backlog"


def pending_level(pending: int) -> PendingLevel:
    if pending < MIN_MESSAGES_TO_SUMMARIZE:
        return PendingLevel.BELOW_MINIMUM
    if pending >= BACKLOG_WARNING_THRESHOLD:
        return PendingLevel.BACKLOG
    return PendingLevel.NORMAL


def meter_fill(pending: int) -> float:
    """Fraction of the backlog threshold, clamped to [0, 1], for the meter bar."""
    return min(pending / BACKLOG_WARNING_THRESHOLD, 1.0)


_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def dom_id(group_jid: str) -> str:
    """A DOM id usable as a CSS selector (JIDs contain '@' and '.')."""
    return "g-" + _UNSAFE_ID_CHARS.sub("-", group_jid)


def related_groups(
    group_jid: str, keys: list[str] | None, index: list[CommunityEntry]
) -> list[str]:
    """Names of other groups sharing at least one community key (same as SQL `&&`)."""
    if not keys:
        return []
    wanted = set(keys)
    return sorted(
        entry.display_name
        for entry in index
        if entry.group_jid != group_jid and wanted.intersection(entry.keys)
    )


def format_relative(value: datetime | None, now: datetime | None = None) -> str:
    """Render a timestamp as Hebrew relative time ("לפני 3 שע׳").

    Relative time sidesteps timezones for display. Naive values (the group
    window columns) are compared with a naive "now", aware values (message
    timestamps) with an aware one - mixing the two would raise TypeError.
    """
    if value is None:
        return "—"
    if now is None:
        now = datetime.now() if value.tzinfo is None else datetime.now(timezone.utc)

    seconds = int((now - value).total_seconds())
    if seconds < _MINUTE:
        return "עכשיו"
    if seconds < _HOUR:
        minutes = seconds // _MINUTE
        return "לפני דקה" if minutes == 1 else f"לפני {minutes} דק׳"
    if seconds < _DAY:
        hours = seconds // _HOUR
        return "לפני שעה" if hours == 1 else f"לפני {hours} שע׳"
    if seconds < _MONTH:
        days = seconds // _DAY
        return "אתמול" if days == 1 else f"לפני {days} ימים"
    months = seconds // _MONTH
    return "לפני חודש" if months == 1 else f"לפני {months} חודשים"
