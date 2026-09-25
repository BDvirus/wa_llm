from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from handler.knowledge_base_answers import AnswerScope, KnowledgeBaseAnswers
from models import Group, KBTopic, Message
from search.hybrid_search import SearchResult, format_search_results_for_prompt
from whatsapp.jid import JID

MODULE = "handler.knowledge_base_answers"


def _kba(session=None) -> KnowledgeBaseAnswers:
    whatsapp = AsyncMock()
    whatsapp.get_my_jid = AsyncMock(
        return_value=JID(user="bot", server="s.whatsapp.net")
    )
    return KnowledgeBaseAnswers(
        session or MagicMock(), whatsapp, AsyncMock(), MagicMock()
    )


def _result(output: str) -> MagicMock:
    r = MagicMock()
    r.output = output
    return r


@pytest.fixture
def pipeline():
    """Patch the LLM, embedding and search steps; return the mocks."""
    with (
        patch.object(
            KnowledgeBaseAnswers,
            "rephrasing_agent",
            AsyncMock(return_value=_result("q")),
        ),
        patch.object(
            KnowledgeBaseAnswers,
            "generation_agent",
            AsyncMock(return_value=_result("answer")),
        ) as gen,
        patch(f"{MODULE}.voyage_embed_text", AsyncMock(return_value=[[0.0] * 1024])),
        patch(f"{MODULE}.hybrid_search", AsyncMock(return_value=[])) as search,
        patch(f"{MODULE}.get_opt_out_map", AsyncMock(return_value={})),
        patch(f"{MODULE}.display_names", AsyncMock(return_value={})) as names,
    ):
        yield MagicMock(generation=gen, search=search, names=names)


class TestScope:
    @pytest.mark.asyncio
    async def test_answer_refuses_an_empty_scope(self, pipeline):
        with pytest.raises(ValueError):
            await _kba().answer(
                query="q",
                history=[],
                sender_jid="u@s.whatsapp.net",
                scope=AnswerScope([]),
            )
        pipeline.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_scope_loads_the_group_when_relationship_is_missing(
        self, pipeline
    ):
        # A Message built in code has no `group`; the scope must still be the group.
        session = MagicMock()
        session.get = AsyncMock(return_value=Group(group_jid="g@g.us"))
        message = Message(
            message_id="1",
            chat_jid="g@g.us",
            group_jid="g@g.us",
            sender_jid="u@s.whatsapp.net",
            text="?",
        )

        assert await _kba(session)._group_scope(message) == ["g@g.us"]

    @pytest.mark.asyncio
    async def test_group_scope_includes_community_groups(self, pipeline):
        group = Group(group_jid="g@g.us", community_keys=["k"])
        with patch.object(
            Group,
            "get_related_community_groups",
            AsyncMock(return_value=[Group(group_jid="c@g.us")]),
        ):
            message = Message(
                message_id="1",
                chat_jid="g@g.us",
                sender_jid="u@s.whatsapp.net",
                text="?",
            )
            message.group = group
            assert await _kba()._group_scope(message) == ["g@g.us", "c@g.us"]

    @pytest.mark.asyncio
    async def test_message_without_any_group_is_not_answered(self, pipeline):
        kba = _kba()
        kba.respond = AsyncMock()  # type: ignore[method-assign]
        dm = Message(
            message_id="1",
            chat_jid="u@s.whatsapp.net",
            sender_jid="u@s.whatsapp.net",
            text="?",
        )

        await kba(dm)

        kba.respond.assert_not_awaited()
        pipeline.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_uses_exactly_the_scope(self, pipeline):
        await _kba().answer(
            query="q",
            history=[],
            sender_jid="u@s.whatsapp.net",
            scope=AnswerScope(["a@g.us", "b@g.us"], private=True),
        )
        assert pipeline.search.await_args.kwargs["group_jids"] == ["a@g.us", "b@g.us"]


class TestPrivateMode:
    @pytest.mark.asyncio
    async def test_output_numbers_are_replaced_with_names(self, pipeline):
        pipeline.generation.return_value = _result("ask @972501111111 or 972502222222")
        pipeline.names.return_value = {"972501111111": "Dana"}

        out = await _kba().answer(
            query="q",
            history=[],
            sender_jid="u@s.whatsapp.net",
            scope=AnswerScope(["a@g.us"], private=True),
        )

        assert out == "ask Dana or משתתף"
        assert pipeline.generation.await_args.kwargs["private"] is True

    @pytest.mark.asyncio
    async def test_group_mode_keeps_tags(self, pipeline):
        pipeline.generation.return_value = _result("ask @972501111111")
        out = await _kba().answer(
            query="q",
            history=[],
            sender_jid="u@s.whatsapp.net",
            scope=AnswerScope(["a@g.us"]),
        )
        assert out == "ask @972501111111"
        pipeline.names.assert_not_awaited()


class TestFormatting:
    def _result(
        self, group_jid: str, summary: str, sender: str = "972501111111@s.whatsapp.net"
    ):
        topic = KBTopic(
            id="t",
            group_jid=group_jid,
            subject="Launch",
            summary=summary,
            speakers="",
            embedding=[0.0] * 1024,
        )
        msg = Message(
            message_id="m",
            chat_jid=group_jid,
            group_jid=group_jid,
            sender_jid=sender,
            text="call 972503333333",
        )
        return SearchResult(topic=topic, messages=[msg], vector_distance=0.1)

    def test_labels_topics_with_their_group(self):
        out = format_search_results_for_prompt(
            [self._result("a@g.us", "s")], {}, group_names={"a@g.us": "Founders"}
        )
        assert "## [Founders] Launch" in out

    def test_private_mode_has_no_phone_numbers(self):
        out = format_search_results_for_prompt(
            [self._result("a@g.us", "as @972501111111 said")],
            {"972501111111": "Dana"},
            private=True,
        )
        assert "972" not in out
        assert "Dana" in out and "- Dana:" in out and "משתתף" in out

    def test_group_mode_keeps_numbers(self):
        out = format_search_results_for_prompt([self._result("a@g.us", "s")], {})
        assert "- @972501111111:" in out
