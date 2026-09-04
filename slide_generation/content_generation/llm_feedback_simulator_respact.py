"""
ReSpAct add-on for the LLM-as-user feedback simulator.

This module provides one new function — ``simulate_user_answer`` — used by
the ReSpAct reviser variants when the agent calls ``ask_user`` during a
simulated-feedback round. Instead of returning a static stub, the simulated
user is asked the agent's clarifying question (in the voice of the original
reviewer) and produces a brief, in-character answer.

This module imports ``_dispatch_llm`` from ``llm_feedback_simulator.py`` and
adds the clarifying-answer prompt.
"""

from __future__ import annotations

from typing import Any, Tuple

from slide_generation.content_generation.llm_feedback_simulator import (
    _dispatch_llm,
)


_ANSWER_SYSTEM_PROMPT = (
    "You are simulating a researcher who is reviewing AI-generated slides for "
    "a research paper. The slide-revising agent has paused mid-revision to ask "
    "you a clarifying question. Answer it briefly (1-3 sentences), directly, "
    "and in the voice of a domain-aware reviewer. Make a concrete decision "
    "rather than deferring; if multiple options are reasonable, pick one and "
    "say why. Do NOT restate the question. Do NOT add meta-commentary. Output "
    "plain text only."
)

_ANSWER_USER_TEMPLATE = """\
Original feedback you gave the agent:
{original_feedback}

Context (current state being revised):
{context}

The agent's clarifying question:
{question}

Your answer (1-3 sentences, direct, in-character):"""


def simulate_user_answer(
    args: Any,
    question: str,
    original_feedback: str,
    context: str,
    *,
    max_context_chars: int = 6000,
) -> Tuple[str, int, int]:
    """Generate an in-character answer to the agent's ``ask_user`` question.

    Reuses the simulator's ``_dispatch_llm`` (same backend selection logic
    as the rest of the simulator), so GPT-5 / vLLM / CAMEL paths all work
    without extra wiring.

    Returns ``(answer_text, in_tokens, out_tokens)``. The answer is plain
    text; the caller is responsible for handing it back to the agent as the
    ``ask_user`` tool's observation.
    """
    if len(context) > max_context_chars:
        context = context[:max_context_chars] + "\n…(truncated)"

    user_prompt = _ANSWER_USER_TEMPLATE.format(
        original_feedback=original_feedback or "(no prior feedback recorded)",
        context=context,
        question=question,
    )

    raw, in_tok, out_tok = _dispatch_llm(
        args,
        system_prompt=_ANSWER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )
    answer = (raw or "").strip()
    if not answer:
        answer = "Use your best judgment based on the paper; keep edits conservative."
    return answer, in_tok, out_tok
