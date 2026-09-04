"""
ReSpAct-style interactive outline-feedback reviser.

Exposes ``run_raw_content_feedback_loop`` /
``run_simulated_raw_content_feedback_loop`` over an arXiv-backed search tool.
The reviser runs an interleaved Think / Speak / Act loop (faithful to the
ReSpAct paper's mechanism) instead of a one-shot rewrite.

Per turn the agent may:
  • emit a thought (free-text content; no environment effect)
  • call ``ask_user(question)``  — SPEAK; pauses for a user reply
  • call ``arxiv_search(query, ...)`` — ACT; existing tool, unchanged backend
  • call ``finalize(slides=[...])`` — terminal ACT; returns the revised slides

Budgets: ``max_tool_iters=10`` total turns, ``max_speak=2`` ask_user calls per
session.
"""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from jinja2 import Environment, StrictUndefined

from slide_generation.edit_ops import apply_outline_ops
from slide_generation.toolcall_stats import ToolCallStats, OUTLINE_SECTION

_PROMPT_PATH = Path("prompts/pipeline/raw_content_feedback_agent.yaml")

_PROMPT_CFG = yaml.safe_load(_PROMPT_PATH.read_text(encoding="utf-8"))


class _FeedbackLogger:
    """Append-and-flush text logger for the raw_content feedback session."""

    def __init__(self, log_path: Path) -> None:
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            f"ConvDeck Raw-Content Feedback Session Log (ReSpAct)\n"
            f"Started : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Log path: {log_path}\n"
            + "=" * 70 + "\n",
            encoding="utf-8",
        )

    def _append(self, text: str) -> None:
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def _ts(self) -> str:
        return time.strftime("%H:%M:%S")

    def section(self, title: str) -> None:
        sep = "─" * 70
        self._append(f"\n{sep}\n{title}\n{sep}")

    def event(self, tag: str, msg: str) -> None:
        self._append(f"[{self._ts()}] [{tag}] {msg}")

    def block(self, tag: str, header: str, body: str) -> None:
        self._append(f"[{self._ts()}] [{tag}] {header}")
        for line in body.splitlines():
            self._append(f"    {line}")

    def tool_call(self, name: str, arguments: Dict[str, Any]) -> None:
        self._append(f"[{self._ts()}] [tool_call] → {name}")
        for k, v in arguments.items():
            v_str = str(v)
            if len(v_str) > 400:
                v_str = v_str[:400] + "…"
            self._append(f"    {k}: {v_str}")

    def tool_result(self, name: str, status: str, detail: str = "") -> None:
        self._append(f"[{self._ts()}] [tool_result] ← {name}: {status}")
        if detail:
            for line in detail.splitlines():
                self._append(f"    {line}")

    def summary(self, total_in: int, total_out: int, elapsed: float) -> None:
        sep = "=" * 70
        self._append(
            f"\n{sep}\n"
            f"SESSION SUMMARY\n"
            f"{sep}\n"
            f"Finished : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Elapsed  : {elapsed:.2f}s\n"
            f"Tokens   : in={total_in}  out={total_out}\n"
            f"{sep}"
        )


def _log_path_for(args: Any, *, simulated: bool = False) -> Path:
    paper_name = getattr(args, "paper_name", "paper")
    model_t = getattr(args, "model_name_t", None) or getattr(args, "model", "model")
    model_v = getattr(args, "model_name_v", None) or model_t
    prefix = f"<{model_t}_{model_v}>"
    suffix = (
        "_raw_content_feedback_log_respact_simulated.txt" if simulated
        else "_raw_content_feedback_log_respact.txt"
    )
    return Path(f"contents/{paper_name}/{prefix}{suffix}")


def _stats_path(args: Any) -> Path:
    """Shared per-paper tool-call stats JSON (both revisers write into it)."""
    paper_name = getattr(args, "paper_name", "paper")
    model_t = getattr(args, "model_name_t", None) or getattr(args, "model", "model")
    model_v = getattr(args, "model_name_v", None) or model_t
    prefix = f"<{model_t}_{model_v}>"
    return Path(f"contents/{paper_name}/{prefix}_toolcall_stats.json")


def _format_arxiv_result_for_log(tool_result: Dict[str, Any]) -> str:
    if "error" in tool_result and tool_result.get("error"):
        return f"ERROR: {tool_result['error']}"
    lines = [f"query: {tool_result.get('query', '')}"]
    papers = tool_result.get("papers") or []
    lines.append(f"papers ({len(papers)}):")
    for p in papers:
        title = p.get("title", "")
        abstract = (p.get("abstract") or "").replace("\n", " ")
        if len(abstract) > 200:
            abstract = abstract[:200] + "…"
        lines.append(f"  - [{p.get('index')}] {title}")
        if abstract:
            lines.append(f"      {abstract}")
    chunks = tool_result.get("chunks") or []
    lines.append(f"chunks ({len(chunks)}):")
    for c in chunks:
        body = (c.get("text") or "").replace("\n", " ").strip()
        if len(body) > 300:
            body = body[:300] + "…"
        lines.append(f"  • paper={c.get('paper_title','?')!r}")
        lines.append(f"      {body}")
    return "\n".join(lines)


# ── Prompt: base prompt + ReSpAct addendum ──────────────────────────────────

_RESPACT_ADDENDUM = """\

ReSpAct protocol — IMPORTANT
You operate as a Think/Speak/Act agent. EVERY turn has exactly two parts,
in this order:

  PART 1 — THINK: First, write 1-3 sentences of plain-text reasoning in your
           message content: what the feedback is asking, what you intend to
           do about it, and why. This is mandatory on EVERY turn.
  PART 2 — SPEAK or ACT: Then call EXACTLY ONE of the tools below.

So every turn is "think, then act" (or "think, then speak"). NEVER call a
tool with empty message content — the think text always rides in the same
turn as the tool call.

The tools (the SPEAK / ACT part):

  • ask_user(question)        — SPEAK. Use ONLY when the user's request is
                                genuinely ambiguous and you cannot make a safe
                                guess. Hard limit: at most 2 ask_user calls per
                                session.
  • arxiv_search(query, ...)  — ACT. Existing tool for fetching background.
  • finalize(slides=[...])    — TERMINAL ACT. You MUST end the session by
                                calling finalize with the full revised slide
                                list. Do not return slides as plain JSON in
                                content — finalize is the only valid way to
                                terminate.

Budget: at most 4 turns total (each turn = one tool call). Plan accordingly:
prefer one search + one finalize, or just finalize directly when no search is
needed. Reserve ask_user for genuine ambiguity.

The slides argument to finalize must be a list of objects with exactly the
keys title, content, discussion_idea — same schema as the input.
"""

_SYSTEM_PROMPT = _PROMPT_CFG["system_prompt"] + _RESPACT_ADDENDUM


# ── ReSpAct addendum — edit-ops variant (--use_edit_ops) ─────────────────────

_RESPACT_ADDENDUM_OPS = """

ReSpAct protocol — IMPORTANT
You operate as a Think/Speak/Act agent. EVERY turn has exactly two parts,
in this order:

  PART 1 — THINK: First, write 1-3 sentences of plain-text reasoning in your
           message content: what the feedback asks and which edits you will
           make. Mandatory on EVERY turn.
  PART 2 — SPEAK or ACT: Then call EXACTLY ONE of the tools below.

The tools:

  • ask_user(question)        — SPEAK. Use ONLY for genuine ambiguity.
                                At most 1 ask_user call per session.
  • arxiv_search(query, ...)  — ACT. Fetch background literature.
  • apply_edits(edits=[...])  — ACT. Apply localized edit operations to the
                                outline. Repeatable.
  • finish()                  — TERMINAL ACT. Call with no arguments once all
                                edits are applied. Ends the session.

CRITICAL — how to revise:
  1. Do NOT regenerate the whole slide list. Emit edit OPERATIONS describing
     ONLY what changes. Every slide you do not touch is preserved verbatim.
  2. Address slides by their EXACT title; 'index' (0-based) also works but
     titles are stable across edits.
  3. apply_edits returns, per op, whether it applied; failed ops carry an
     error and the observation lists the outline's CURRENT titles. If any op
     failed, fix it using those exact titles and call apply_edits again with
     ONLY the corrected ops.
  4. For merge_slides: if you OMIT 'content' on the merged slide, the system
     concatenates the source slides' content for you — so you only need to
     supply 'title' and 'discussion_idea'. Do not re-type the paragraphs.
  5. When done, call finish(). If you never call finish, the edits you already
     applied are still saved.

Op vocabulary (each object in 'edits' has an 'op' key):
  edit_slide   {title|index, new_title?, content?, discussion_idea?}
  retitle      {title|index, new_title}
  add_slide    {slide:{title,content,discussion_idea}, at_index?}
  remove_slide {title|index}
  move_slide   {title|index, after?|before?|to_index?}
  reorder      {order:[every title|index in new order]}
  split_slide  {title|index, parts:[{title,content,discussion_idea}]}
  merge_slides {targets:[title|index,...], merged:{title,discussion_idea,content?}}

IMPORTANT — renaming: the 'title' field SELECTS a slide, it never renames it.
To rename a slide use the 'retitle' op (or pass 'new_title' to edit_slide).
After a rename, refer to that slide by its NEW title in later ops.

IMPORTANT — moving: to relocate ONE slide use 'move_slide' (e.g.
{op:"move_slide", title:"X", after:"Y"}). Only use 'reorder' for a full
reshuffle — it requires listing EVERY slide exactly once.

Budget: at most 4 turns total. Prefer one apply_edits + finish.
"""

_SYSTEM_PROMPT_OPS = _PROMPT_CFG["system_prompt"] + _RESPACT_ADDENDUM_OPS

_USER_TEMPLATE = Environment(undefined=StrictUndefined).from_string(
    _PROMPT_CFG["template"]
)

_ARXIV_TOOL_CFG = _PROMPT_CFG["arxiv_tool"]
_ARXIV_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": _ARXIV_TOOL_CFG["name"],
        "description": _ARXIV_TOOL_CFG["description"],
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": _ARXIV_TOOL_CFG["parameters"]["query_description"],
                },
                "extraction_query": {
                    "type": "string",
                    "description": _ARXIV_TOOL_CFG["parameters"]["extraction_query_description"],
                },
            },
            "required": ["query"],
        },
    },
}

_ASK_USER_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "SPEAK action. Ask the user a clarifying question when their "
            "feedback is genuinely ambiguous. Use sparingly — at most twice "
            "per session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Concise clarifying question to show the user.",
                },
            },
            "required": ["question"],
        },
    },
}

_FINALIZE_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "finalize",
        "description": (
            "Terminal action. Return the fully revised slide list. "
            "MUST be called to end the session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slides": {
                    "type": "array",
                    "description": "Revised slide list, same schema as input.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                            "discussion_idea": {"type": "string"},
                        },
                        "required": ["title", "content", "discussion_idea"],
                    },
                },
            },
            "required": ["slides"],
        },
    },
}

_TOOL_SPECS = [_ARXIV_TOOL_SPEC, _ASK_USER_TOOL_SPEC, _FINALIZE_TOOL_SPEC]

_APPLY_EDITS_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "apply_edits",
        "description": (
            "ACT action. Apply a batch of localized edit operations to the "
            "outline. May be called more than once. Each item of 'edits' is an "
            "object with an 'op' key. Supported ops and their fields:\n"
            "  edit_slide   {title|index, new_title?, content?, discussion_idea?}\n"
            "  retitle      {title|index, new_title}\n"
            "  add_slide    {slide:{title,content,discussion_idea}, at_index?}\n"
            "  remove_slide {title|index}\n"
            "  move_slide   {title|index, after?|before?|to_index?}\n"
            "  reorder      {order:[every title|index in new order]}\n"
            "  split_slide  {title|index, parts:[{title,content,discussion_idea}]}\n"
            "  merge_slides {targets:[title|index,...], "
            "merged:{title,discussion_idea,content?}}\n"
            "Address slides by their EXACT title. To RENAME a slide use "
            "'retitle' (or edit_slide's 'new_title') — the 'title' field only "
            "selects a slide. To MOVE one slide use 'move_slide'; reserve "
            "'reorder' for a full reshuffle. Emit ops ONLY for what changes — "
            "untouched slides are preserved automatically. For merge_slides, "
            "omit 'content' to have source content concatenated automatically. "
            "The tool reports which ops applied and which failed (with the "
            "current titles); fix and re-emit only the failed ones."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": "List of edit-operation objects, applied in order.",
                    "items": {"type": "object"},
                },
            },
            "required": ["edits"],
        },
    },
}

_FINISH_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "Terminal action. Call with no arguments once all requested edits "
            "have been applied via apply_edits. Ends the session."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

_TOOL_SPECS_OPS = [_ARXIV_TOOL_SPEC, _ASK_USER_TOOL_SPEC,
                   _APPLY_EDITS_TOOL_SPEC, _FINISH_TOOL_SPEC]


def _format_slides_for_review(slides: List[Dict[str, Any]]) -> str:
    lines = []
    for i, s in enumerate(slides, 1):
        title = s.get("title", "")
        idea = (s.get("discussion_idea") or "").strip()
        lines.append(f"[{i}] {title}")
        if idea:
            lines.append(f"    idea: {idea}")
    return "\n".join(lines)


def _call_arxiv_tool(query: str, extraction_query: Optional[str] = None) -> Dict[str, Any]:
    """Back the reviser's ``arxiv_search`` tool: retrieve related papers and
    passages for ``query`` and return a compact ``{query, papers, chunks}`` dict.

    Retrieval is served by the Paperclip service (``tools.paperclip_search``),
    which needs a ``PAPERCLIP_MCP_API_KEY`` (see https://paperclip.gxl.ai). Any
    backend error is caught and returned as an ``{"error": ...}`` payload so a
    retrieval failure never aborts the feedback loop.
    """
    try:
        from slide_generation.tools.paperclip_search import paper_retrieval_pipeline
        result = paper_retrieval_pipeline(query, extraction_query=extraction_query)
    except Exception as e:
        return {"error": f"arxiv_search failed: {e}", "papers": [], "chunks": []}

    papers = result.get("papers", []) or []
    global_chunks = result.get("global_top_chunks", []) or []

    paper_summaries = [
        {
            "index": p.get("index"),
            "title": p.get("title", ""),
            "abstract": (p.get("abstract") or "")[:600],
        }
        for p in papers
    ]

    title_by_chunk = {}
    for p in papers:
        for tc in p.get("top_chunks", []) or []:
            title_by_chunk[tc] = p.get("title", "")

    chunks_with_attribution = []
    for ch in global_chunks[:4]:
        chunks_with_attribution.append({
            "paper_title": title_by_chunk.get(ch, ""),
            "text": ch,
        })

    return {
        "query": query,
        "papers": paper_summaries,
        "chunks": chunks_with_attribution,
    }


def _build_client_and_model(args) -> Tuple[Any, str]:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise ImportError("openai package is required for raw_content_feedback_agent") from e

    api_key = os.environ.get("OPENAI_API_KEY") or "dummy_key"
    base_url = os.environ.get("OPENAI_BASE_URL")
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    model_name = getattr(args, "model_name_t", None) or getattr(args, "model", "gpt-4o-mini")
    return client, model_name


AskUserHandler = Callable[[str], Tuple[str, int, int]]


def _stdin_ask_user_handler(question: str) -> Tuple[str, int, int]:
    print("\n" + "─" * 70)
    print(f"[respact SPEAK] Agent asks: {question}")
    print("─" * 70)
    answer = input("Your answer: ").strip()
    return (answer or "(no answer provided; use your best judgment)"), 0, 0


def _make_simulated_ask_user_handler(
    args,
    original_feedback: str,
    slide_content: List[Dict[str, Any]],
    paper_summary: str,
) -> AskUserHandler:
    """Build a handler that routes ask_user to the LLM-as-user simulator."""
    from slide_generation.content_generation.llm_feedback_simulator_respact import (
        simulate_user_answer,
    )

    def _handler(question: str) -> Tuple[str, int, int]:
        outline_view = _format_slides_for_review(slide_content)
        context = (
            "Current outline (slide titles + ideas):\n"
            f"{outline_view}\n\n"
            f"Paper summary excerpt:\n{(paper_summary or '')[:3000]}"
        )
        return simulate_user_answer(
            args,
            question=question,
            original_feedback=original_feedback,
            context=context,
        )

    return _handler


def _run_one_revision(
    client: Any,
    model_name: str,
    slides: List[Dict[str, Any]],
    user_feedback: str,
    *,
    logger: Optional[_FeedbackLogger] = None,
    ask_user_handler: AskUserHandler = _stdin_ask_user_handler,
    max_tool_iters: int = 10,
    max_speak: int = 2,
    use_edit_ops: bool = False,
    stats: Optional[ToolCallStats] = None,
) -> Tuple[List[Dict[str, Any]], int, int, Dict[str, float]]:
    """ReSpAct loop: at most ``max_tool_iters`` turns, ``max_speak`` ask_user calls.

    Returns ``(new_slides, in_tokens, out_tokens, speak_part)`` where
    ``speak_part`` is ``{in, out, time}`` for the ask_user (LLM-as-user)
    sub-calls — these are part of ``in_tokens``/``out_tokens`` but tracked
    separately so the caller can attribute them to the reviewer rather than
    the reviser.

    When ``use_edit_ops`` is True the reviser emits localized edit operations
    (apply_edits/finish) instead of regenerating the whole slide list; edits
    accumulate into ``working_slides`` across turns. If the agent never
    terminates, the edits applied so far (or, on the legacy path, the original
    slides) are returned.
    """

    user_payload = {
        "current_slides": slides,
        "user_feedback": user_feedback,
    }

    rendered_user = _USER_TEMPLATE.render(
        user_payload_json=json.dumps(user_payload, ensure_ascii=False, indent=2),
    )

    system_prompt = _SYSTEM_PROMPT_OPS if use_edit_ops else _SYSTEM_PROMPT
    tool_specs = _TOOL_SPECS_OPS if use_edit_ops else _TOOL_SPECS

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": rendered_user},
    ]

    # Edit-ops path: edits accumulate into working_slides across turns; `finish`
    # promotes it to the returned result.
    working_slides: List[Dict[str, Any]] = copy.deepcopy(slides)

    in_tokens_total = 0
    out_tokens_total = 0
    speak_count = 0
    speak_in = speak_out = 0
    speak_time = 0.0

    if stats is not None:
        stats.start_round()

    for turn_idx in range(1, max_tool_iters + 1):
        resp = client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=tool_specs,
            tool_choice="auto",
            reasoning_effort="low",
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            in_tokens_total += getattr(usage, "prompt_tokens", 0) or 0
            out_tokens_total += getattr(usage, "completion_tokens", 0) or 0

        choice = resp.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, "tool_calls", None) or []
        thought_text = (msg.content or "").strip()

        if stats is not None:
            stats.record_turn(bool(thought_text))

        if logger is not None and thought_text:
            logger.block("respact_think", f"turn {turn_idx}", thought_text)

        # Every action turn is expected to carry preceding reasoning ("think").
        # Warn (do not block) when the agent called a tool without any.
        if tool_calls and not thought_text and logger is not None:
            logger.event(
                "respact_warn",
                f"turn {turn_idx}: action taken with no think text "
                "(reasoning is expected before every action)",
            )

        if not tool_calls:
            if logger is not None:
                logger.event("respact_warn",
                             f"turn {turn_idx}: no tool call emitted; nudging finalize")
            messages.append({
                "role": "assistant",
                "content": thought_text,
            })
            if use_edit_ops:
                nudge = (
                    "You did not call any tool. You MUST call exactly one of "
                    "ask_user / arxiv_search / apply_edits / finish. Apply your "
                    "changes with apply_edits, then call finish."
                )
            else:
                nudge = (
                    "You did not call any tool. You MUST call exactly one of "
                    "ask_user / arxiv_search / finalize. If you have enough "
                    "information, call finalize now with the revised slides."
                )
            messages.append({"role": "user", "content": nudge})
            continue

        # Append assistant turn with tool calls
        messages.append({
            "role": "assistant",
            "content": thought_text,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        })

        finalized: Optional[List[Dict[str, Any]]] = None

        for tc in tool_calls:
            name = tc.function.name
            try:
                tc_args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                tc_args = {}

            if stats is not None:
                stats.record_tool(name)

            if name == "apply_edits":
                edits = tc_args.get("edits") or []
                working_slides, op_results = apply_outline_ops(working_slides, edits)
                if stats is not None:
                    stats.record_apply_edits(op_results)
                ok_results = [r for r in op_results if r.ok]
                bad_results = [r for r in op_results if not r.ok]
                if logger is not None:
                    logger.tool_call("apply_edits", {"ops": len(edits)})
                    detail = "\n".join(
                        f"[{r.op}] {r.target}: "
                        + ("ok" if r.ok else f"FAILED — {r.error}")
                        + (f"  (warn: {'; '.join(r.warnings)})" if r.warnings else "")
                        for r in op_results
                    )
                    logger.tool_result(
                        "apply_edits",
                        f"applied={len(ok_results)} failed={len(bad_results)}",
                        detail,
                    )
                observation = {
                    "applied": len(ok_results),
                    "failed": [
                        {"op": r.op, "target": r.target, "error": r.error}
                        for r in bad_results
                    ],
                    "warnings": [
                        {"op": r.op, "target": r.target, "warnings": r.warnings}
                        for r in ok_results if r.warnings
                    ],
                }
                # When an op failed, hand the agent the outline's current titles
                # so it can retry against the real state in one shot instead of
                # guessing.
                if bad_results:
                    observation["current_titles"] = [
                        str(s.get("title", "")) for s in working_slides
                    ]
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(observation, ensure_ascii=False),
                })
                continue

            if name == "finish":
                finalized = working_slides
                if logger is not None:
                    logger.tool_call("finish", {"slides": len(working_slides)})
                    logger.tool_result("finish", "ok",
                                        f"returned {len(working_slides)} slides")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"ok": True}),
                })
                continue

            if name == "finalize":
                raw_slides = tc_args.get("slides")
                if isinstance(raw_slides, list) and raw_slides:
                    cleaned: List[Dict[str, Any]] = []
                    for s in raw_slides:
                        if not isinstance(s, dict):
                            continue
                        cleaned.append({
                            "title": str(s.get("title", "")),
                            "content": str(s.get("content", "")),
                            "discussion_idea": str(s.get("discussion_idea", "")),
                        })
                    if cleaned:
                        finalized = cleaned
                if logger is not None:
                    logger.tool_call("finalize", {"slides_count": len(raw_slides) if isinstance(raw_slides, list) else 0})
                    logger.tool_result(
                        "finalize",
                        "ok" if finalized else "rejected (empty/invalid)",
                        f"returned {len(finalized)} slides" if finalized else "",
                    )
                tool_obs = {
                    "ok": finalized is not None,
                    "slides_received": len(raw_slides) if isinstance(raw_slides, list) else 0,
                }
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_obs, ensure_ascii=False),
                })
                continue

            if name == "ask_user":
                question = (tc_args.get("question") or "").strip()
                if not question:
                    answer = "(empty question; please proceed with best judgment)"
                    if logger is not None:
                        logger.tool_call("ask_user", {"question": "(empty)"})
                        logger.tool_result("ask_user", "empty")
                elif speak_count >= max_speak:
                    answer = (
                        "Speak budget exhausted. Do not ask another question. "
                        "Use your best judgment and call finalize."
                    )
                    if logger is not None:
                        logger.tool_call("ask_user", {"question": question})
                        logger.tool_result("ask_user", "rejected (over budget)")
                else:
                    speak_count += 1
                    if logger is not None:
                        logger.tool_call("ask_user", {"question": question})
                    _spk_start = time.time()
                    answer, ans_in, ans_out = ask_user_handler(question)
                    _spk_dt = time.time() - _spk_start
                    in_tokens_total += ans_in
                    out_tokens_total += ans_out
                    speak_in += ans_in
                    speak_out += ans_out
                    speak_time += _spk_dt
                    if logger is not None:
                        logger.tool_result(
                            "ask_user", "ok",
                            f"question: {question}\nanswer: {answer}\n"
                            f"answer_tokens: in={ans_in} out={ans_out}",
                        )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"answer": answer}, ensure_ascii=False),
                })
                continue

            if name == "arxiv_search":
                query = (tc_args.get("query") or "").strip()
                extraction_query = (tc_args.get("extraction_query") or "").strip() or None
                print(
                    f"[raw_content_feedback_agent_respact] arxiv_search "
                    f"query={query!r} extraction_query={extraction_query!r}"
                )
                if logger is not None:
                    logger.tool_call(
                        "arxiv_search",
                        {"query": query, "extraction_query": extraction_query},
                    )
                tool_result = (
                    _call_arxiv_tool(query, extraction_query)
                    if query else {"error": "empty query"}
                )
                if logger is not None:
                    if tool_result.get("error"):
                        logger.tool_result("arxiv_search", "error", tool_result.get("error", ""))
                    else:
                        n_papers = len(tool_result.get("papers") or [])
                        n_chunks = len(tool_result.get("chunks") or [])
                        logger.tool_result(
                            "arxiv_search",
                            f"ok (papers={n_papers}, chunks={n_chunks})",
                            _format_arxiv_result_for_log(tool_result),
                        )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                })
                continue

            # Unknown tool
            if logger is not None:
                logger.tool_call(name, {"raw_arguments": tc.function.arguments})
                logger.tool_result(name, "rejected", "unknown tool")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps({"error": f"unknown tool {name}"}),
            })

        if finalized is not None:
            speak_part = {"in": speak_in, "out": speak_out, "time": speak_time}
            return finalized, in_tokens_total, out_tokens_total, speak_part

    speak_part = {"in": speak_in, "out": speak_out, "time": speak_time}
    if use_edit_ops:
        # Edit-ops path: agent never called finish, but any edits it applied
        # are already in working_slides — keep them.
        print("[raw_content_feedback_agent_respact] Reached max turns without "
              "finish; keeping the edits applied so far.")
        if logger is not None:
            logger.event("respact_warn",
                         f"reached max_tool_iters={max_tool_iters} without finish; "
                         "keeping the edits applied so far")
        return working_slides, in_tokens_total, out_tokens_total, speak_part
    print("[raw_content_feedback_agent_respact] Reached max turns without finalize; keeping previous slides.")
    if logger is not None:
        logger.event("respact_warn",
                     f"reached max_tool_iters={max_tool_iters} without finalize")
    return slides, in_tokens_total, out_tokens_total, speak_part


def run_raw_content_feedback_loop(
    slide_content: List[Dict[str, Any]],
    args: Any,
) -> Tuple[List[Dict[str, Any]], int, int, Dict[str, Dict[str, float]]]:
    """Interactive ReSpAct outline-feedback loop.

    Same outer flow as the simulated feedback loop; the per-round reviser is the ReSpAct agent above.
    Returns ``(slide_content, in_tokens, out_tokens, breakdown)``. The human
    path has no LLM reviewer (ask_user goes to stdin), so only a ``_reviser``
    entry is emitted.
    """
    client, model_name = _build_client_and_model(args)

    in_tokens_total = 0
    out_tokens_total = 0
    reviser_in = reviser_out = 0
    reviser_time = 0.0
    round_n = 0
    started = time.time()

    log_path = _log_path_for(args)
    logger = _FeedbackLogger(log_path)
    stats = ToolCallStats(mode="interactive")
    logger.event("init",
                 f"respact  model={model_name}  paper={getattr(args, 'paper_name', '?')}  "
                 f"slides={len(slide_content)}  max_tool_iters=10  max_speak=2")
    logger.section("INITIAL OUTLINE")
    logger.block("initial", f"{len(slide_content)} slides", _format_slides_for_review(slide_content))
    logger.block("initial_json", "raw_content_rst (input)",
                 json.dumps(slide_content, ensure_ascii=False, indent=2))
    print(f"[raw_content_feedback_agent_respact] Logging session to {log_path}")

    try:
        from slide_generation.interaction_logger import log_user_feedback as _log_uf
    except Exception:
        _log_uf = None

    while True:
        print("\n" + "=" * 70)
        print(f"RAW CONTENT RST REVIEW (round {round_n})  [ReSpAct]")
        print("=" * 70)
        print(_format_slides_for_review(slide_content))
        print("=" * 70)
        print("\nPress Enter or type 'ok' to approve.")
        print("Otherwise, enter feedback to revise the slides.")
        print("Tip: the agent may ask you clarifying questions (max 2 per round).")

        feedback = input(
            "\nEnter feedback to revise the slides "
            "(or press Enter / type 'ok' to approve): "
        ).strip()

        approved = feedback.lower() in ("", "ok", "approve", "approved", "done", "yes", "y")
        if _log_uf is not None:
            try:
                _log_uf(
                    stage="raw_content_feedback_approval" if approved else "raw_content_feedback_input",
                    content=feedback or "(empty)",
                )
            except Exception:
                pass

        if approved:
            print("[raw_content_feedback_agent_respact] Slides approved.")
            logger.event("approve", f"after {round_n} revision round(s)")
            break

        round_n += 1
        logger.section(f"ROUND {round_n} — USER FEEDBACK")
        logger.block("user_feedback", f"round {round_n}", feedback)

        print(f"[raw_content_feedback_agent_respact] Revising (round {round_n})...")
        _rev_start = time.time()
        slide_content, in_t, out_t, speak_part = _run_one_revision(
            client, model_name, slide_content, feedback,
            logger=logger,
            ask_user_handler=_stdin_ask_user_handler,
            max_tool_iters=10,
            max_speak=2,
            use_edit_ops=getattr(args, "use_edit_ops", False),
            stats=stats,
        )
        _rev_dt = time.time() - _rev_start
        in_tokens_total += in_t
        out_tokens_total += out_t
        # Human path: ask_user goes to stdin (0 tokens). Reviser bucket is the
        # whole revision minus the stdin wait time captured in speak_part.
        reviser_in += in_t - speak_part["in"]
        reviser_out += out_t - speak_part["out"]
        reviser_time += max(0.0, _rev_dt - speak_part["time"])

        logger.section(f"ROUND {round_n} — REVISED OUTLINE")
        logger.block("revised", f"{len(slide_content)} slides",
                     _format_slides_for_review(slide_content))
        logger.block("revised_json", f"raw_content_rst (round {round_n})",
                     json.dumps(slide_content, ensure_ascii=False, indent=2))
        logger.event("tokens", f"round {round_n}: in={in_t} out={out_t} time={_rev_dt:.2f}s")

    logger.section("FINAL OUTLINE")
    logger.block("final", f"{len(slide_content)} slides",
                 _format_slides_for_review(slide_content))
    logger.block("final_json", "raw_content_rst (final)",
                 json.dumps(slide_content, ensure_ascii=False, indent=2))
    logger.summary(in_tokens_total, out_tokens_total, time.time() - started)
    stats.write(_stats_path(args), OUTLINE_SECTION,
                paper=getattr(args, "paper_name", ""))

    breakdown = {
        "raw_content_feedback_reviser": {
            "in": reviser_in, "out": reviser_out, "time": reviser_time,
        },
    }
    return slide_content, in_tokens_total, out_tokens_total, breakdown


def run_simulated_raw_content_feedback_loop(
    slide_content: List[Dict[str, Any]],
    paper_summary: str,
    args: Any,
) -> Tuple[List[Dict[str, Any]], int, int, Dict[str, Dict[str, float]]]:
    """Simulated ReSpAct outline-feedback loop.

    The reviewer is the existing simulator; the reviser is the ReSpAct agent.
    ask_user calls are auto-answered by the LLM-as-user simulator. Returns
    ``(slide_content, in_tokens, out_tokens, breakdown)``; the reviewer bucket
    includes both the verdict simulator and the ask_user answerer.
    """
    from slide_generation.content_generation.llm_feedback_simulator import (
        simulate_outline_feedback,
        MAX_SIMULATED_ROUNDS,
    )

    client, model_name = _build_client_and_model(args)

    in_tokens_total = 0
    out_tokens_total = 0
    reviewer_in = reviewer_out = 0
    reviewer_time = 0.0
    reviser_in = reviser_out = 0
    reviser_time = 0.0
    started = time.time()

    log_path = _log_path_for(args, simulated=True)
    logger = _FeedbackLogger(log_path)
    stats = ToolCallStats(mode="simulated")
    logger.event(
        "init",
        f"respact simulated  model={model_name}  paper={getattr(args, 'paper_name', '?')}  "
        f"slides={len(slide_content)}  max_rounds={MAX_SIMULATED_ROUNDS}  "
        f"max_tool_iters=10  max_speak=2",
    )
    logger.section("INITIAL OUTLINE")
    logger.block("initial", f"{len(slide_content)} slides",
                 _format_slides_for_review(slide_content))
    logger.block("initial_json", "raw_content_rst (input)",
                 json.dumps(slide_content, ensure_ascii=False, indent=2))
    print(f"[raw_content_feedback_agent_respact] (simulated) Logging session to {log_path}")

    # Feedback history given to the reviewer (feedback-giving agent) only —
    # so it can see what it already asked and avoid repeating/contradicting.
    reviewer_prior_feedback: List[str] = []

    for round_n in range(1, MAX_SIMULATED_ROUNDS + 1):
        print(f"\n[respact simulated] Outline review round {round_n}/{MAX_SIMULATED_ROUNDS}...")
        logger.section(f"ROUND {round_n} — REVIEWER VERDICT")
        feedback, rev_in, rev_out, dt = simulate_outline_feedback(
            slide_content, paper_summary, args, round_number=round_n,
            prior_feedback=reviewer_prior_feedback,
        )
        in_tokens_total += rev_in
        out_tokens_total += rev_out
        reviewer_in += rev_in
        reviewer_out += rev_out
        reviewer_time += dt
        logger.event("reviewer_tokens",
                     f"round {round_n}: in={rev_in} out={rev_out} time={dt:.2f}s")

        if feedback is None:
            print("[respact simulated] Outline approved by simulated reviewer.")
            logger.event("approve", f"after {round_n - 1} revision round(s)")
            break

        logger.block("reviewer_feedback", f"round {round_n}", feedback)
        reviewer_prior_feedback.append(feedback)

        print(f"[raw_content_feedback_agent_respact] Revising (simulated round {round_n})...")
        sim_handler = _make_simulated_ask_user_handler(
            args, original_feedback=feedback,
            slide_content=slide_content, paper_summary=paper_summary,
        )
        _rev_start = time.time()
        slide_content, rwr_in, rwr_out, speak_part = _run_one_revision(
            client, model_name, slide_content, feedback,
            logger=logger,
            ask_user_handler=sim_handler,
            max_tool_iters=10,
            max_speak=2,
            use_edit_ops=getattr(args, "use_edit_ops", False),
            stats=stats,
        )
        _rev_dt = time.time() - _rev_start
        in_tokens_total += rwr_in
        out_tokens_total += rwr_out
        # ask_user tokens/time inside _run_one_revision are the LLM-as-user
        # answerer → attribute to the reviewer; the rest is the reviser.
        reviewer_in += speak_part["in"]
        reviewer_out += speak_part["out"]
        reviewer_time += speak_part["time"]
        reviser_in += rwr_in - speak_part["in"]
        reviser_out += rwr_out - speak_part["out"]
        reviser_time += max(0.0, _rev_dt - speak_part["time"])

        logger.section(f"ROUND {round_n} — REVISED OUTLINE")
        logger.block("revised", f"{len(slide_content)} slides",
                     _format_slides_for_review(slide_content))
        logger.block("revised_json", f"raw_content_rst (round {round_n})",
                     json.dumps(slide_content, ensure_ascii=False, indent=2))
        logger.event("reviser_tokens",
                     f"round {round_n}: in={rwr_in} out={rwr_out} time={_rev_dt:.2f}s")
    else:
        logger.event("max_rounds",
                     f"reached cap of {MAX_SIMULATED_ROUNDS} revision rounds without approval")

    # Persist the reviewer's feedback history for logging / offline analysis.
    try:
        model_t = getattr(args, "model_name_t", None) or getattr(args, "model", "model")
        model_v = getattr(args, "model_name_v", None) or model_t
        hist_path = Path(
            f"contents/{getattr(args, 'paper_name', 'paper')}/"
            f"<{model_t}_{model_v}>_outline_feedback_history.json"
        )
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        hist_path.write_text(
            json.dumps(reviewer_prior_feedback, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.event("feedback_history",
                     f"wrote {len(reviewer_prior_feedback)} item(s) → {hist_path}")
    except Exception as exc:
        logger.event("respact_warn", f"could not write feedback history: {exc}")

    logger.section("FINAL OUTLINE")
    logger.block("final", f"{len(slide_content)} slides",
                 _format_slides_for_review(slide_content))
    logger.block("final_json", "raw_content_rst (final)",
                 json.dumps(slide_content, ensure_ascii=False, indent=2))
    logger.summary(in_tokens_total, out_tokens_total, time.time() - started)
    stats.write(_stats_path(args), OUTLINE_SECTION,
                paper=getattr(args, "paper_name", ""))

    breakdown = {
        "raw_content_feedback_reviewer": {
            "in": reviewer_in, "out": reviewer_out, "time": reviewer_time,
        },
        "raw_content_feedback_reviser": {
            "in": reviser_in, "out": reviser_out, "time": reviser_time,
        },
    }
    return slide_content, in_tokens_total, out_tokens_total, breakdown


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--paper_name", type=str, default="open_vocab")
    parser.add_argument("--model_name_t", type=str, default="gpt-5")
    parser.add_argument("--model_name_v", type=str, default="gpt-5")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--input", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument(
        "--use_edit_ops", action="store_true",
        help="Reviser emits localized edit ops (apply_edits/finish) instead of "
             "regenerating the whole slide list.",
    )
    cli_args = parser.parse_args()
    if cli_args.model is None:
        cli_args.model = cli_args.model_name_t

    from dotenv import load_dotenv
    load_dotenv()

    if cli_args.input:
        rst_cache = cli_args.input
    else:
        prefix = f"<{cli_args.model}_{cli_args.model}>"
        rst_cache = f"contents/{cli_args.paper_name}/{prefix}_raw_content_rst.json"

    if not os.path.exists(rst_cache):
        raise SystemExit(f"raw_content_rst file not found: {rst_cache}")

    print(f"Loading raw_content_rst from {rst_cache}")
    with open(rst_cache, "r", encoding="utf-8") as f:
        slide_content = json.load(f)

    print(f"Loaded {len(slide_content)} slides. Starting ReSpAct feedback loop using model '{cli_args.model_name_t}'.")
    revised, in_tokens, out_tokens, breakdown = run_raw_content_feedback_loop(slide_content, cli_args)
    print(f"Breakdown: {breakdown}")

    out_path = cli_args.output or rst_cache
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(revised, f, indent=4, ensure_ascii=False)

    print(f"\nDone. Tokens — in: {in_tokens}, out: {out_tokens}")
    print(f"Saved revised raw_content_rst to {out_path}")
