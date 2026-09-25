"""Keyword routing for private chats.

Deterministic on purpose: no LLM call just to decide what the user wants,
which matters under a daily quota and on rate-limited free models.
"""

import re
from enum import Enum


class Command(str, Enum):
    HELP = "help"
    CATCH_UP = "catch_up"
    SEARCH = "search"
    QUESTION = "question"


_HELP = {"עזרה", "help", "/help", "?"}
_CATCH_UP = {"מה פספסתי", "/missed", "missed"}
# "חפש ..." / "חיפוש: ..." / "/search ..." / "search ..." - a whole word, so
# "חפשתי" or "searching" stay questions.
_SEARCH = re.compile(
    r"^(?:חפש|חיפוש|/search|search)(?=$|[\s:])[\s:]*(?P<query>.*)$", re.IGNORECASE
)


def parse_command(text: str) -> tuple[Command, str]:
    """Return the command and its argument (the search terms, or the question)."""
    stripped = text.strip()
    key = stripped.lower().rstrip("?").strip() or stripped.lower()
    if stripped.lower() in _HELP or key in _HELP:
        return Command.HELP, ""
    if key in _CATCH_UP:
        return Command.CATCH_UP, ""
    match = _SEARCH.match(stripped)
    if match:
        return Command.SEARCH, match.group("query").strip()
    return Command.QUESTION, stripped
