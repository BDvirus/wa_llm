import pytest

from utils.redact import phone_users, redact_phones


class TestPhoneUsers:
    def test_finds_tagged_bare_and_plus_numbers(self):
        text = "per @972536150150 and 972501234567, call +972509999999"
        assert phone_users(text) == {"972536150150", "972501234567", "972509999999"}

    @pytest.mark.parametrize(
        "text",
        ["050-1234567", "972 50 123 4567", "+972 (50) 123-4567", "(050) 123.4567"],
    )
    def test_finds_numbers_written_with_separators(self, text):
        assert len(phone_users(text)) == 1

    def test_separated_number_is_keyed_by_its_digits(self):
        assert phone_users("call 972-50-123-4567") == {"972501234567"}

    @pytest.mark.parametrize(
        "text", ["in 2026 we paid 1500", "id 12345678", "", "25.09.2026", "10:00-12:00"]
    )
    def test_ignores_short_numbers(self, text):
        assert phone_users(text) == set()


class TestRedactPhones:
    def test_replaces_known_numbers_with_names(self):
        assert (
            redact_phones("thanks @972536150150!", {"972536150150": "Dana"})
            == "thanks Dana!"
        )

    def test_unknown_numbers_get_a_neutral_label(self):
        assert redact_phones("ask 972501234567", {}) == "ask משתתף"

    def test_leaves_text_without_numbers_alone(self):
        assert redact_phones("nothing here, 2026", {"1": "x"}) == "nothing here, 2026"

    def test_every_occurrence_is_replaced(self):
        out = redact_phones(
            "@972536150150 then +972536150150", {"972536150150": "Dana"}
        )
        assert out == "Dana then Dana"

    def test_separated_numbers_are_replaced_whole(self):
        assert redact_phones("תתקשרו ל-050-1234567 מחר", {}) == "תתקשרו ל-משתתף מחר"
        assert redact_phones("+972 50-123-4567.", {"972501234567": "Dana"}) == "Dana."
