from datetime import datetime, timedelta, timezone

import pytest

from admin.display import (
    PendingLevel,
    dom_id,
    format_relative,
    meter_fill,
    pending_level,
    related_groups,
)
from admin.forms import parse_community_keys
from admin.queries import CommunityEntry


class TestParseCommunityKeys:
    def test_splits_trims_and_keeps_order(self):
        assert parse_community_keys(" genai , founders,tech ") == [
            "genai",
            "founders",
            "tech",
        ]

    def test_drops_empty_and_duplicate_keys(self):
        assert parse_community_keys("a,, ,b,a, b") == ["a", "b"]

    @pytest.mark.parametrize("raw", ["", "   ", ",", " , ,"])
    def test_empty_input_clears_keys(self, raw):
        assert parse_community_keys(raw) is None


class TestFormatRelative:
    NOW_AWARE = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    NOW_NAIVE = datetime(2026, 9, 25, 12, 0)

    def test_none_renders_placeholder(self):
        assert format_relative(None, now=self.NOW_AWARE) == "—"

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(seconds=30), "עכשיו"),
            (timedelta(minutes=1), "לפני דקה"),
            (timedelta(minutes=7), "לפני 7 דק׳"),
            (timedelta(hours=1), "לפני שעה"),
            (timedelta(hours=5), "לפני 5 שע׳"),
            (timedelta(days=1), "אתמול"),
            (timedelta(days=4), "לפני 4 ימים"),
            (timedelta(days=31), "לפני חודש"),
            (timedelta(days=95), "לפני 3 חודשים"),
        ],
    )
    def test_buckets(self, delta, expected):
        assert format_relative(self.NOW_AWARE - delta, now=self.NOW_AWARE) == expected

    def test_naive_values_compare_against_naive_now(self):
        # last_ingest / last_summary_sync are naive columns; they must not be
        # compared against an aware "now" (that raises TypeError).
        assert (
            format_relative(self.NOW_NAIVE - timedelta(hours=2), now=self.NOW_NAIVE)
            == "לפני 2 שע׳"
        )

    def test_future_values_render_as_now(self):
        assert (
            format_relative(self.NOW_AWARE + timedelta(minutes=5), now=self.NOW_AWARE)
            == "עכשיו"
        )


class TestPendingLevel:
    @pytest.mark.parametrize(
        ("pending", "expected"),
        [
            (0, PendingLevel.BELOW_MINIMUM),
            (14, PendingLevel.BELOW_MINIMUM),
            (15, PendingLevel.NORMAL),
            (499, PendingLevel.NORMAL),
            (500, PendingLevel.BACKLOG),
        ],
    )
    def test_levels(self, pending, expected):
        assert pending_level(pending) is expected


class TestDomId:
    def test_is_stable_and_selector_safe(self):
        assert dom_id("120363041234567890@g.us") == "g-120363041234567890-g-us"

    def test_keeps_hyphenated_group_ids(self):
        assert (
            dom_id("972501234567-1612345678@g.us") == "g-972501234567-1612345678-g-us"
        )


class TestMeterFill:
    @pytest.mark.parametrize(
        ("pending", "expected"), [(0, 0.0), (250, 0.5), (500, 1.0), (9000, 1.0)]
    )
    def test_is_clamped_fraction_of_backlog_threshold(self, pending, expected):
        assert meter_fill(pending) == expected


class TestRelatedGroups:
    INDEX = [
        CommunityEntry("a@g.us", "Alpha", ["genai", "founders"]),
        CommunityEntry("b@g.us", "Beta", ["genai"]),
        CommunityEntry("c@g.us", "Gamma", ["other"]),
    ]

    def test_lists_groups_sharing_any_key_excluding_self(self):
        assert related_groups("a@g.us", ["genai", "founders"], self.INDEX) == ["Beta"]

    def test_no_keys_means_no_related(self):
        assert related_groups("a@g.us", None, self.INDEX) == []
