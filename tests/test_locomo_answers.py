"""Answer-accuracy tests for LoCoMo question-answering evaluation.

These tests are fully hermetic — no network calls. A StubProvider simulates
the LLM for answer generation so the scoring pipeline is exercised
deterministically.
"""
from __future__ import annotations

import json

import pytest

from ah.core.models import LLMResponse
from ah.research.locomo import (
    AnswerMetrics,
    Conversation,
    DialogueTurn,
    Question,
    StubProvider,
    evaluate_answers,
    generate_answer,
    token_f1,
)


# ---------------------------------------------------------------------------
# Token F1 — the core scoring function
# ---------------------------------------------------------------------------


def test_token_f1_perfect_match():
    assert token_f1("Toronto is the city", "Toronto is the city") == 1.0


def test_token_f1_partial_overlap():
    # prediction: "the city is Toronto" (4 tokens)
    # ground truth: "Toronto is the city" (4 tokens)
    # common: 4 → precision=1, recall=1 → F1=1.0 (order-independent)
    assert token_f1("the city is Toronto", "Toronto is the city") == 1.0


def test_token_f1_no_overlap():
    assert token_f1("completely different words", "nothing alike here") == 0.0


def test_token_f1_partial_score():
    # prediction: "the big city" (3 tokens)
    # ground truth: "the city" (2 tokens)
    # common: 2 → precision=2/3, recall=2/2=1 → F1=2*(2/3*1)/(2/3+1)=0.8
    score = token_f1("the big city", "the city")
    assert score == pytest.approx(0.8, abs=0.01)


def test_token_f1_empty_prediction():
    assert token_f1("", "some answer") == 0.0


def test_token_f1_empty_ground_truth():
    assert token_f1("some answer", "") == 0.0


def test_token_f1_both_empty():
    assert token_f1("", "") == 0.0


def test_token_f1_case_insensitive():
    assert token_f1("TORONTO", "toronto") == 1.0


def test_token_f1_handles_punctuation():
    assert token_f1("Hello, world!", "hello world") == 1.0


def test_token_f1_handles_non_string_ground_truth():
    """LoCoMo ground-truth answers can be integers (e.g. a count or year)."""
    assert token_f1("3", 3) == 1.0
    assert token_f1("three", 3) == 0.0


def test_token_f1_handles_none_ground_truth():
    assert token_f1("anything", None) == 0.0


# ---------------------------------------------------------------------------
# StubProvider — deterministic offline LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_provider_returns_configured_answer():
    provider = StubProvider(answer="Toronto")
    response = await provider.complete(
        [{"role": "user", "content": "Where did Ada move?"}]
    )
    assert isinstance(response, LLMResponse)
    assert response.content == "Toronto"
    assert response.model == "stub"


@pytest.mark.asyncio
async def test_stub_provider_tracks_call_count():
    provider = StubProvider(answer="test")
    await provider.complete([{"role": "user", "content": "q1"}])
    await provider.complete([{"role": "user", "content": "q2"}])
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_stub_provider_can_simulate_wrong_answer():
    provider = StubProvider(answer="Paris")
    response = await provider.complete([{"role": "user", "content": "q"}])
    assert response.content == "Paris"


# ---------------------------------------------------------------------------
# generate_answer — builds prompt and calls provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_answer_builds_prompt_with_evidence():
    captured: dict = {}

    class CapturingProvider(StubProvider):
        async def complete(self, messages, **kwargs):
            captured["messages"] = messages
            return await super().complete(messages, **kwargs)

    provider = CapturingProvider(answer="Toronto")
    result = await generate_answer(
        provider,
        "Where did Ada move?",
        ["Ada: I moved to Toronto last year."],
    )
    assert result == "Toronto"
    # The prompt should contain the question and evidence
    prompt_text = " ".join(m["content"] for m in captured["messages"])
    assert "Where did Ada move?" in prompt_text
    assert "Toronto" in prompt_text


@pytest.mark.asyncio
async def test_generate_answer_with_empty_evidence():
    provider = StubProvider(answer="I don't know")
    result = await generate_answer(provider, "What?", [])
    assert result == "I don't know"


@pytest.mark.asyncio
async def test_generate_answer_with_empty_question():
    provider = StubProvider(answer="answer")
    result = await generate_answer(provider, "", ["some evidence"])
    assert result == "answer"


@pytest.mark.asyncio
async def test_generate_answer_handles_none_content():
    """A provider can return None content (e.g. an empty/filtered completion);
    that must yield an empty answer, not crash the corpus run."""
    from ah.core.models import LLMResponse

    class NoneProvider:
        async def complete(self, *a, **k):
            return LLMResponse(content=None, model="stub", usage={})

    result = await generate_answer(NoneProvider(), "Where?", ["evidence"])
    assert result == ""


@pytest.mark.asyncio
async def test_generate_answer_retries_transient_failure():
    """A corpus run must survive transient provider errors (429s) by retrying
    with backoff instead of aborting the whole measurement."""
    from ah.core.models import LLMResponse

    class FlakyProvider:
        def __init__(self):
            self.calls = 0

        async def complete(self, *a, **k):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("429 Too Many Requests")
            return LLMResponse(content="Toronto", model="stub", usage={})

    provider = FlakyProvider()
    result = await generate_answer(provider, "Where?", ["evidence"], retries=4, backoff=0.0)
    assert result == "Toronto"
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_generate_answer_gives_up_after_retries():
    """After exhausting retries the error propagates so a run is not silently
    scored against empty answers."""
    from ah.core.models import LLMResponse

    class DeadProvider:
        def __init__(self):
            self.calls = 0

        async def complete(self, *a, **k):
            self.calls += 1
            raise RuntimeError("429 Too Many Requests")

    provider = DeadProvider()
    with pytest.raises(RuntimeError, match="429"):
        await generate_answer(provider, "Where?", ["evidence"], retries=3, backoff=0.0)
    assert provider.calls == 3


# ---------------------------------------------------------------------------
# evaluate_answers — full pipeline
# ---------------------------------------------------------------------------


def _make_conversation() -> Conversation:
    return Conversation(
        turns=(
            DialogueTurn("D1:1", "Ada", "I moved to Toronto in 2020."),
            DialogueTurn("D1:2", "Ben", "That is great!"),
            DialogueTurn("D1:3", "Ada", "I work as a software engineer."),
        ),
        questions=(
            Question("Where did Ada move?", ("D1:1",), 1, "Toronto"),
            Question("What does Ada do?", ("D1:3",), 1, "software engineer"),
        ),
    )


class MappingStubProvider(StubProvider):
    """Stub that returns different answers based on the question text."""

    def __init__(self, answer_map: dict[str, str]) -> None:
        super().__init__()
        self.answer_map = answer_map

    async def complete(self, messages, **kwargs):
        # Extract the question from the prompt
        prompt = messages[-1]["content"]
        for question_text, answer in self.answer_map.items():
            if question_text in prompt:
                self.answer = answer
                break
        return await super().complete(messages, **kwargs)


@pytest.mark.asyncio
async def test_evaluate_answers_perfect_score():
    """Stub returns the exact ground-truth answer → F1 = 1.0."""
    provider = MappingStubProvider(
        {
            "Where did Ada move?": "Toronto",
            "What does Ada do?": "software engineer",
        }
    )
    metrics = await evaluate_answers([_make_conversation()], provider)
    assert metrics.questions == 2
    assert metrics.mean_f1 == 1.0
    assert metrics.exact_match_rate == 1.0


@pytest.mark.asyncio
async def test_evaluate_answers_wrong_answer_scores_zero():
    """Stub returns a wrong answer → F1 = 0.0."""
    provider = StubProvider(answer="Paris")
    metrics = await evaluate_answers([_make_conversation()], provider)
    assert metrics.questions == 2
    assert metrics.mean_f1 == 0.0
    assert metrics.exact_match_rate == 0.0


@pytest.mark.asyncio
async def test_evaluate_answers_partial_credit():
    """Stub returns a partially-correct answer for one question, wrong for the other."""
    provider = StubProvider(answer="Toronto city")
    metrics = await evaluate_answers([_make_conversation()], provider)
    assert metrics.questions == 2
    # Q1: "Toronto city" vs "Toronto" → F1 = 2*(1*0.5)/(1+0.5) = 0.667
    # Q2: "Toronto city" vs "software engineer" → F1 = 0.0
    # Mean = 0.333
    assert 0.3 < metrics.mean_f1 < 0.4


@pytest.mark.asyncio
async def test_evaluate_answers_skips_questions_without_evidence():
    """Adversarial questions (no evidence) are skipped."""
    conv = Conversation(
        turns=(DialogueTurn("D1:1", "Ada", "I moved to Toronto."),),
        questions=(
            Question("Where did Ada move?", ("D1:1",), 1, "Toronto"),
            Question("adversarial question", (), 5),  # no evidence
        ),
    )
    provider = StubProvider(answer="Toronto")
    metrics = await evaluate_answers([conv], provider)
    assert metrics.questions == 1
    assert metrics.skipped_questions == 1


@pytest.mark.asyncio
async def test_evaluate_answers_missing_evidence_text():
    """When evidence IDs don't match any turn, evidence text is empty."""
    conv = Conversation(
        turns=(DialogueTurn("D1:1", "Ada", "I moved to Toronto."),),
        questions=(
            Question("Where?", ("D9:9",), 1, "Toronto"),  # non-existent dia_id
        ),
    )
    provider = StubProvider(answer="Toronto")
    # Should not crash — missing evidence is handled gracefully
    metrics = await evaluate_answers([conv], provider)
    assert metrics.questions == 1


@pytest.mark.asyncio
async def test_evaluate_answers_empty_conversations():
    """No conversations → ValueError."""
    provider = StubProvider(answer="test")
    with pytest.raises(ValueError, match="no conversations"):
        await evaluate_answers([], provider)


@pytest.mark.asyncio
async def test_evaluate_answers_all_questions_skipped():
    """All questions lack evidence → ValueError."""
    conv = Conversation(
        turns=(DialogueTurn("D1:1", "Ada", "text"),),
        questions=(Question("adversarial", (), 5),),
    )
    provider = StubProvider(answer="test")
    with pytest.raises(ValueError, match="no questions with evidence"):
        await evaluate_answers([conv], provider)


@pytest.mark.asyncio
async def test_evaluate_answers_multiple_conversations():
    conv1 = _make_conversation()
    conv2 = Conversation(
        turns=(DialogueTurn("D2:1", "Bob", "I like pizza."),),
        questions=(Question("What does Bob like?", ("D2:1",), 1, "pizza"),),
    )
    provider = StubProvider(answer="Toronto")
    metrics = await evaluate_answers([conv1, conv2], provider)
    assert metrics.questions == 3


@pytest.mark.asyncio
async def test_evaluate_answers_returns_per_question_scores():
    """Metrics include per-question detail for debugging."""
    provider = StubProvider(answer="Toronto")
    metrics = await evaluate_answers([_make_conversation()], provider)
    assert hasattr(metrics, "scores")
    assert len(metrics.scores) == 2
    # Each score entry has question, prediction, ground_truth, f1
    for entry in metrics.scores:
        assert "question" in entry
        assert "prediction" in entry
        assert "ground_truth" in entry
        assert "f1" in entry


# ---------------------------------------------------------------------------
# AnswerMetrics dataclass
# ---------------------------------------------------------------------------


def test_answer_metrics_to_dict():
    metrics = AnswerMetrics(
        questions=10,
        skipped_questions=2,
        mean_f1=0.75,
        exact_match_rate=0.5,
        scores=[],
    )
    d = metrics.to_dict()
    assert d["questions"] == 10
    assert d["skipped_questions"] == 2
    assert d["mean_f1"] == 0.75
    assert d["exact_match_rate"] == 0.5


# ---------------------------------------------------------------------------
# Integration: load_locomo + evaluate_answers with a JSON file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_with_locomo_json(tmp_path):
    """Load a LoCoMo-format JSON and evaluate answers."""
    path = tmp_path / "locomo.json"
    path.write_text(
        json.dumps(
            [
                {
                    "conversation": {
                        "session_1": [
                            {"dia_id": "D1:1", "speaker": "Ada", "text": "I moved to Toronto."},
                        ],
                    },
                    "qa": [
                        {
                            "question": "Where did Ada move?",
                            "answer": "Toronto",
                            "evidence": ["D1:1"],
                            "category": 1,
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    from ah.research.locomo import load_locomo

    conversations = load_locomo(path)
    provider = StubProvider(answer="Toronto")
    metrics = await evaluate_answers(conversations, provider)
    assert metrics.questions == 1
    assert metrics.mean_f1 == 1.0
