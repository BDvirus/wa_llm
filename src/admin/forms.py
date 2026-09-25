"""Parsing of admin form input into model values."""


def parse_community_keys(raw: str) -> list[str] | None:
    """Turn comma-separated input into an ordered, de-duplicated key list.

    Blank input clears the keys (None), matching the column's nullable default.
    """
    keys = list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    return keys or None
