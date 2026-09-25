"""Keep phone numbers out of text shown in private chats.

Topic summaries and group messages carry raw numbers (`@972...` tags are
restored into summaries at ingest). In a group everyone already sees them;
in a private chat they would leak numbers - including ones WhatsApp hides in
LID-addressed groups - so they are swapped for display names.
"""

import re

# 9-15 digits, optionally tagged (@) or international (+), and allowing the
# usual separators between digits ("050-1234567", "+972 (50) 123 4567").
# Shorter runs (dates, prices, ids) are left alone. Errs toward privacy: a
# space-separated run of years like "2024 2025 2026" is redacted too.
_PHONE = re.compile(r"(?<!\d)[@+]?\(?\d(?:[ \-.()]{0,2}\d){8,14}(?!\d)")
_NON_DIGIT = re.compile(r"\D")

UNKNOWN_PERSON = "משתתף"


def phone_users(text: str) -> set[str]:
    """The digit strings of every phone-like number in the text."""
    return {_digits(m.group()) for m in _PHONE.finditer(text)}


def redact_phones(text: str, names: dict[str, str]) -> str:
    """Replace each phone-like number with its display name, or a neutral label."""
    return _PHONE.sub(lambda m: names.get(_digits(m.group()), UNKNOWN_PERSON), text)


def _digits(match: str) -> str:
    return _NON_DIGIT.sub("", match)
