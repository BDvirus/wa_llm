"""Hebrew relative time for user-facing text. Pure, no I/O."""

from datetime import datetime, timezone

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR
_MONTH = 30 * _DAY


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
