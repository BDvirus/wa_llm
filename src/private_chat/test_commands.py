from datetime import datetime, timedelta, timezone

import pytest

from private_chat.commands import Command, parse_command
from private_chat.formatting import SearchHit, format_search_hits


class TestParseCommand:
    @pytest.mark.parametrize("text", ["עזרה", "help", "/help", " ? ", "HELP"])
    def test_help(self, text):
        assert parse_command(text) == (Command.HELP, "")

    @pytest.mark.parametrize("text", ["מה פספסתי", "מה פספסתי?", "/missed", "missed"])
    def test_catch_up(self, text):
        assert parse_command(text) == (Command.CATCH_UP, "")

    @pytest.mark.parametrize(
        ("text", "query"),
        [
            ("חפש קורס פייתון", "קורס פייתון"),
            ("חיפוש: meetup", "meetup"),
            ("/search  docker compose ", "docker compose"),
            ("search Traefik", "Traefik"),
        ],
    )
    def test_search_keeps_the_query_as_typed(self, text, query):
        assert parse_command(text) == (Command.SEARCH, query)

    @pytest.mark.parametrize("text", ["חפש", "/search", "חיפוש:"])
    def test_search_without_terms_is_flagged(self, text):
        assert parse_command(text) == (Command.SEARCH, "")

    @pytest.mark.parametrize(
        "text", ["מה החלטנו על המיטאפ?", "חפשתי את זה אתמול", "helpful tips?"]
    )
    def test_anything_else_is_a_question(self, text):
        assert parse_command(text) == (Command.QUESTION, text.strip())


class TestFormatSearchHits:
    NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

    def test_lists_group_sender_age_and_snippet(self):
        hits = [
            SearchHit(
                "Founders", "Dana", self.NOW - timedelta(hours=3), "we meet on Thursday"
            )
        ]
        out = format_search_hits(hits, "meet", now=self.NOW)
        assert "Founders" in out and "Dana" in out and "לפני 3 שע׳" in out
        assert "we meet on Thursday" in out

    def test_long_messages_are_cut(self):
        hits = [SearchHit("G", "Dana", self.NOW, "x" * 500)]
        assert "x" * 201 not in format_search_hits(hits, "x", now=self.NOW)

    def test_no_hits_says_so(self):
        assert "לא נמצאו" in format_search_hits([], "nothing", now=self.NOW)
