"""
JS-stage feedback loop.

Mirrors ``feedback_respact.py``'s public surface, but the operating artifact
is the generated PptxGenJS ``.js`` file (not ``slide_plan.json``), and the
reviser exposes JS-level edit tools instead of slide-plan ops:

  • ``patch_js(edits=[{find, replace}, ...])``  — free-form find/replace.
  • ``set_slide_override(items=[{slide_index, override}, ...])`` — write
    structured per-slide overrides into the SLIDE_OVERRIDES dict.
  • ``edit_slide_plan(ops=[...])`` — content-level slide-plan ops applied to
    the SLIDE_PLAN literal embedded in the JS.
  • ``finish()`` — terminal action; re-runs Node to regenerate the .pptx.

Public API (same shape as feedback_respact):
  • ``apply_user_feedback_js(args, js_path)``
  • ``apply_simulated_feedback_js(args, js_path)``
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from jinja2 import Environment, StrictUndefined
from openai import OpenAI

from utils.core.helpers import get_json_from_response
from utils.llm.chat import extract_text_from_responses
from slide_generation.interaction_logger import log_user_feedback
from slide_generation.edit_ops_js import (
    apply_js_patches,
    apply_slide_overrides,
    apply_slide_plan_edits,
    get_slide_plan,
    repair_js_latex,
    _parse_slide_overrides,
)
from slide_generation.content_generation.feedback_respact import (
    _FeedbackLogger,
    _load_raw_content,
    _load_image_registry,
    format_plan_summary,
)


PROMPT_PATH = Path("prompts/pipeline/js_feedback_revision.yaml")


# ── Paths ──────────────────────────────────────────────────────────────────

def _log_path(args) -> Path:
    prefix = f"<{args.model_name_t}_{args.model_name_v}>"
    return Path(f"contents/{args.paper_name}/{prefix}_feedback_log_js.txt")


def _snapshot_root(args) -> Path:
    return Path("tmp") / args.paper_name / "js_feedback"


def _default_pptx_path(args, theme_id: int, design_id: int) -> Path:
    return Path(
        f"contents/{args.paper_name}/"
        f"{args.model_name_t}_{args.model_name_v}_output_slides_"
        f"pptxgenjs_theme{theme_id}_design{design_id}.pptx"
    )


# ── Tool specs (Responses API) ─────────────────────────────────────────────

def _tool_patch_js() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": "patch_js",
        "description": (
            "Free-form find/replace edits on the JS file. Each item has "
            "'find' and 'replace' strings; 'find' must match exactly once "
            "in the current JS (include enough surrounding context). Use "
            "for global JS changes (default font sizes, bodyOpts template "
            "defaults, imageBoxes defaults, color tweaks). For per-slide "
            "visual overrides prefer set_slide_override; for content "
            "changes prefer edit_slide_plan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": "List of {find, replace} edits, applied in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "find": {"type": "string"},
                            "replace": {"type": "string"},
                        },
                        "required": ["find", "replace"],
                    },
                },
            },
            "required": ["edits"],
        },
    }


def _tool_set_slide_override() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": "set_slide_override",
        "description": (
            "Set per-slide visual overrides written into the SLIDE_OVERRIDES "
            "dict in the JS. 'items' is a list of "
            "{slide_index (1-based), override} entries. Supported override "
            "keys: bullet_font_size (number), title_font_size (number), "
            "body_xywh ([x,y,w,h] inches), image_xywh ([[x,y,w,h]|null,...] "
            "one per visual on the slide), hide_figure (bool). Pass an "
            "empty override object to clear a slide's overrides."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "slide_index": {"type": "integer"},
                            "override": {"type": "object"},
                        },
                        "required": ["slide_index", "override"],
                    },
                },
            },
            "required": ["items"],
        },
    }


def _tool_edit_slide_plan() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": "edit_slide_plan",
        "description": (
            "Apply content-level edit ops to the SLIDE_PLAN literal in the "
            "JS. Each item of 'ops' is an object with an 'op' key. "
            "Supported ops and their exact fields:\n"
            "  set_bullets   {slide_title, bullets:[str|{text,sub}], column?}\n"
            "  set_paragraph {slide_title, paragraph, column?}\n"
            "  set_template  {slide_title, template_id}\n"
            "  remove_figure {slide_title}  (auto-sets T1_TextOnly)\n"
            "  retitle       {slide_title, new_title}\n"
            "  add_slide     {slide:{section,subsection,template_id,bullets,"
            "paragraph,images,tables,reference}, after_title?|before_title?|at_index?}\n"
            "  remove_slide  {slide_title}\n"
            "  move_slide    {slide_title, after_title?|before_title?|at_index?}\n"
            "  reorder       {order:[every slide_title in new order]}\n"
            "  split_slide   {slide_title, halves:[{subsection,bullets?|paragraph?}]}\n"
            "  merge_slides  {slide_titles:[...], merged_subsection, bullets?|paragraph?}\n"
            "Address slides by their EXACT subsection title — never by index. "
            "For add_slide, 'slide' MUST be an OBJECT with the listed fields, "
            "not a bare string. To RENAME a slide use 'retitle'. To MOVE one "
            "slide use 'move_slide'; reserve 'reorder' for a full reshuffle. "
            "The tool returns which ops applied and which failed; fix and "
            "re-emit only the failed ones."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "description": "List of edit-op objects (op + fields).",
                    "items": {"type": "object"},
                },
            },
            "required": ["ops"],
        },
    }


def _tool_ask_user() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": "ask_user",
        "description": (
            "Ask the user a clarifying question when the feedback is "
            "genuinely ambiguous. Use sparingly — at most twice per session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
            },
            "required": ["question"],
        },
    }


def _tool_finish() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": "finish",
        "description": (
            "Terminal action. Call with no arguments once all requested "
            "edits have been applied. Saves the JS and re-runs Node."
        ),
        "parameters": {"type": "object", "properties": {}},
    }


_RESPACT_ADDENDUM_JS = """

ReSpAct protocol — IMPORTANT
EVERY turn has exactly two parts:

  PART 1 — THINK: 1-3 sentences of plain-text reasoning in your message
           content describing what you are about to do and why.
  PART 2 — ACT or SPEAK: call EXACTLY ONE tool.

Tools:
  • edit_slide_plan(ops=[...])     — ACT. Content edits (split, merge,
    add bullets, retitle, etc.). Repeatable.
  • set_slide_override(items=[...]) — ACT. Per-slide visual overrides
    (font sizes, body_xywh, image_xywh, hide_figure). Repeatable.
  • patch_js(edits=[...])          — ACT. Free-form JS find/replace for
    GLOBAL changes the other two tools cannot express. Repeatable.
  • ask_user(question)             — SPEAK. Genuine ambiguity only.
    At most 2 per session.
  • finish()                       — TERMINAL ACT. Call with no
    arguments when done. The system then runs Node to regenerate the
    .pptx and the session ends.

There is NO web_search tool. Do not pretend to search — use the
provided raw_content and your own knowledge.

Budget: at most 5 turns total.
"""


# ── Plan summary helper for the prompt ─────────────────────────────────────

def _extract_js_block(js_text: str, header_re: str, terminator_re: str = r"^}\s*$") -> str:
    """Slice ``js_text`` from a header-line regex to the first terminator line."""
    import re as _re
    lines = js_text.splitlines()
    h = _re.compile(header_re)
    t = _re.compile(terminator_re)
    start = -1
    for i, ln in enumerate(lines):
        if h.search(ln):
            start = i
            break
    if start < 0:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if t.search(lines[j]):
            end = j + 1
            break
    return "\n".join(lines[start:end])


def _slim_js_surface(js_text: str) -> str:
    """Return only the JS sections ``patch_js`` realistically touches:
      font config, SLIDE_OVERRIDES current value, palette C, bodyOpts,
      imageBoxes. The full js_text is still applied to behind the scenes
      via apply_js_patches — the agent just sees a slim view when picking
      its ``find`` strings.
    """
    parts = [
        "// === Slim patchable surface ===",
        "// (The full JS file is still applied to behind the scenes; this slim view "
        "is just what you need to write `find` strings for `patch_js`. For per-slide "
        "edits use set_slide_override; for content edits use edit_slide_plan — "
        "neither needs JS source.)",
    ]
    fc = _extract_js_block(js_text, r"^let TITLE_FONT_SIZE\s*=", r"^let SUB_BULLET_FONT_SIZE\s*=")
    if fc:
        parts.append("// --- Font config (defaults) ---\n" + fc)
    so = _extract_js_block(js_text, r"^const SLIDE_OVERRIDES\s*=", r"^};\s*$")
    if so:
        parts.append("// --- SLIDE_OVERRIDES (current value) ---\n" + so)
    pal = _extract_js_block(js_text, r"^const C\s*=", r"^};\s*$")
    if pal:
        parts.append("// --- Palette ---\n" + pal)
    bo = _extract_js_block(js_text, r"^function bodyOpts\(", r"^}\s*$")
    if bo:
        parts.append("// --- bodyOpts (default body rect per template_id) ---\n" + bo)
    ib = _extract_js_block(js_text, r"^function imageBoxes\(", r"^}\s*$")
    if ib:
        parts.append("// --- imageBoxes (default image rect per template_id) ---\n" + ib)
    return "\n\n".join(parts)


def _plan_summary_from_js(js_text: str) -> str:
    plan = get_slide_plan(js_text)
    if not plan:
        return "(could not parse SLIDE_PLAN from JS)"
    return format_plan_summary(plan)


# ── Stdin / simulated ask_user handlers ────────────────────────────────────

AskUserHandler = Callable[[str], Tuple[str, int, int]]


def _stdin_ask_user_handler(question: str) -> Tuple[str, int, int]:
    print("\n" + "─" * 70)
    print(f"[js feedback SPEAK] Agent asks: {question}")
    print("─" * 70)
    answer = input("Your answer: ").strip()
    return (answer or "(no answer provided; use your best judgment)"), 0, 0


def _make_simulated_ask_user_handler(
    args,
    original_feedback: str,
    js_text: str,
    raw_content: Any,
) -> AskUserHandler:
    from slide_generation.content_generation.llm_feedback_simulator_respact import (
        simulate_user_answer,
    )

    def _handler(question: str) -> Tuple[str, int, int]:
        plan = get_slide_plan(js_text) or {}
        context = (
            "Slide plan summary:\n"
            f"{format_plan_summary(plan)}\n\n"
            "Raw content keys: "
            f"{list(raw_content.keys()) if isinstance(raw_content, dict) else type(raw_content).__name__}"
        )
        return simulate_user_answer(
            args,
            question=question,
            original_feedback=original_feedback,
            context=context,
        )

    return _handler


# ── Node re-render ─────────────────────────────────────────────────────────

def _run_node(js_path: Path, logger: _FeedbackLogger) -> Tuple[bool, str]:
    """Run ``node {js_path}``. Returns ``(ok, stderr_or_stdout)``."""
    try:
        proc = subprocess.run(
            ["node", str(js_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=300,
        )
    except Exception as exc:
        logger.event("node_error", f"failed to invoke node: {exc}")
        return False, str(exc)
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        logger.event("node_error", f"node exit={proc.returncode}; stderr head: {err[:300]}")
        return False, err or out
    logger.event("node_ok", f"node stdout tail: {out[-200:]}")
    return True, out


def _repair_js_latex_inplace(js_path: Path, logger: _FeedbackLogger) -> bool:
    """Repair eaten-backslash LaTeX commands in the embedded SLIDE_PLAN and
    re-run node if anything changed. Returns True if a repair was applied."""
    js_text = js_path.read_text(encoding="utf-8")
    new_js, changed = repair_js_latex(js_text)
    if not changed:
        return False
    js_path.write_text(new_js, encoding="utf-8")
    logger.event("latex_repair", "restored backslashes in SLIDE_PLAN; re-rendering")
    _run_node(js_path, logger)
    return True


# ── Snapshot per round ─────────────────────────────────────────────────────

def _snapshot_round(
    args, round_num: int, js_path: Path, pptx_path: Path, logger: _FeedbackLogger,
) -> None:
    snap = _snapshot_root(args) / f"feedback{round_num}"
    snap.mkdir(parents=True, exist_ok=True)
    try:
        if js_path.is_file():
            shutil.copy2(str(js_path), str(snap / js_path.name))
        if pptx_path.is_file():
            shutil.copy2(str(pptx_path), str(snap / pptx_path.name))
    except Exception as exc:
        logger.event("snapshot", f"round={round_num}: copy failed: {exc}")
        return
    # Render PPTX → PNGs for debugging.
    try:
        from slide_generation.content_generation.llm_feedback_simulator import (
            _render_pptx_to_slide_images,
        )
        # High-res render for on-screen human review in the study app. The
        # defaults produce small VLM thumbnails that look pixelated when shown
        # full-size, so ask for a larger, higher-quality image here.
        images = _render_pptx_to_slide_images(
            str(pptx_path), max_slides=40,
            dpi=150, max_w=1600, max_h=900, quality=90,
        )
    except Exception as exc:
        logger.event("snapshot", f"round={round_num}: png render failed: {exc}")
        images = []
    pngs_dir = snap / "slides"
    pngs_dir.mkdir(parents=True, exist_ok=True)
    for i, blob in enumerate(images, 1):
        try:
            (pngs_dir / f"slide_{i:04d}.jpg").write_bytes(blob)
        except Exception:
            pass
    logger.event("snapshot", f"round={round_num}: archived → {snap}  imgs={len(images)}")


# ── Reviser (ReSpAct multi-turn on GPT-5 only; fallback = single oneshot) ──

def _load_revision_prompt() -> Dict[str, str]:
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(f"JS revision prompt not found: {PROMPT_PATH}")
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _revise_once_respact_gpt5(
    args,
    js_text: str,
    raw_content: Any,
    user_feedback: str,
    logger: _FeedbackLogger,
    *,
    ask_user_handler: AskUserHandler,
    image_registry: Optional[set] = None,
    max_tool_iters: int = 10,
    max_speak: int = 2,
) -> Tuple[str, int, int, float, Dict[str, float]]:
    """Returns ``(new_js, in_tok, out_tok, dt, speak_part)``."""
    prompt_cfg = _load_revision_prompt()
    jinja_env = Environment(undefined=StrictUndefined)
    template = jinja_env.from_string(prompt_cfg["template"])

    raw_content_str = (
        json.dumps(raw_content, indent=2)
        if not isinstance(raw_content, str)
        else raw_content
    )
    existing_overrides, _ = _parse_slide_overrides(js_text)
    plan = get_slide_plan(js_text) or {}
    # Strip _resolved_visuals (long absolute paths repeated per slide) — the
    # reviser does not need them; figure filenames are in images/tables.
    slim_plan = copy.deepcopy(plan)
    for s in (slim_plan.get("slides") or []):
        s.pop("_resolved_visuals", None)
    slide_plan_json = json.dumps(slim_plan, indent=2, ensure_ascii=False)
    rendered = template.render(
        user_feedback=user_feedback,
        raw_content_json=raw_content_str,
        slide_plan_json=slide_plan_json,
        slide_overrides_json=json.dumps(existing_overrides, indent=2),
        js_surface=_slim_js_surface(js_text),
    )

    base_prompt = prompt_cfg.get("system_prompt") or ""
    system_prompt = base_prompt + _RESPACT_ADDENDUM_JS
    model_name = getattr(args, "model_name_v", None) or getattr(args, "model", "gpt-5")

    tools = [
        _tool_edit_slide_plan(),
        _tool_set_slide_override(),
        _tool_patch_js(),
        _tool_ask_user(),
        _tool_finish(),
    ]

    working = js_text
    client = OpenAI()
    start = time.time()
    in_tok_total = out_tok_total = 0
    speak_count = 0
    speak_in = speak_out = 0
    speak_time = 0.0
    finished = False

    next_input: Any = rendered
    previous_response_id: Optional[str] = None

    for turn_idx in range(1, max_tool_iters + 1):
        kwargs = dict(
            model=model_name,
            input=next_input,
            tools=tools,
            reasoning={"effort": "low"},
            text={"verbosity": "low"},
        )
        if previous_response_id is None:
            kwargs["instructions"] = system_prompt
        else:
            kwargs["previous_response_id"] = previous_response_id

        try:
            response = client.responses.create(**kwargs)
        except Exception as exc:
            logger.event("js_respact_error", f"turn {turn_idx}: responses.create failed: {exc}")
            break

        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tok_total += getattr(usage, "input_tokens", 0) or 0
            out_tok_total += getattr(usage, "output_tokens", 0) or 0

        previous_response_id = response.id
        try:
            any_thought = (extract_text_from_responses(response) or "").strip()
        except Exception:
            any_thought = ""
        if any_thought:
            short = any_thought if len(any_thought) < 1000 else any_thought[:1000] + "…"
            logger.block("js_respact_think", f"turn {turn_idx}", short)

        pending_outputs: List[Dict[str, Any]] = []

        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", "") or ""
            if item_type != "function_call":
                continue
            name = getattr(item, "name", "") or ""
            call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
            try:
                fn_args = json.loads(getattr(item, "arguments", "") or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            if name == "patch_js":
                edits = fn_args.get("edits") or []
                working, op_results = apply_js_patches(working, edits)
                ok_n = sum(1 for r in op_results if r.ok)
                bad = [r for r in op_results if not r.ok]
                logger.tool_call("patch_js", {"edits": len(edits)})
                logger.tool_result(
                    "patch_js", f"applied={ok_n} failed={len(bad)}",
                    "\n".join(f"[#{r.index}] {r.target}: " + ("ok" if r.ok else f"FAILED — {r.error}") for r in op_results),
                )
                pending_outputs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({
                        "applied": ok_n,
                        "failed": [{"index": r.index, "target": r.target, "error": r.error} for r in bad],
                    }, ensure_ascii=False),
                })
                continue

            if name == "set_slide_override":
                items = fn_args.get("items") or []
                plan = get_slide_plan(working) or {}
                n_slides = len((plan or {}).get("slides") or []) or None
                working, op_results = apply_slide_overrides(working, items, n_slides=n_slides)
                ok_n = sum(1 for r in op_results if r.ok)
                bad = [r for r in op_results if not r.ok]
                logger.tool_call("set_slide_override", {"items": len(items)})
                logger.tool_result(
                    "set_slide_override", f"applied={ok_n} failed={len(bad)}",
                    "\n".join(f"[#{r.index}] {r.target}: " + ("ok" if r.ok else f"FAILED — {r.error}") for r in op_results),
                )
                pending_outputs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({
                        "applied": ok_n,
                        "failed": [{"index": r.index, "target": r.target, "error": r.error} for r in bad],
                    }, ensure_ascii=False),
                })
                continue

            if name == "edit_slide_plan":
                ops = fn_args.get("ops") or []
                working, op_results, _new_plan = apply_slide_plan_edits(
                    working, ops, image_registry,
                )
                ok_n = sum(1 for r in op_results if r.ok)
                bad = [r for r in op_results if not r.ok]
                logger.tool_call("edit_slide_plan", {"ops": len(ops)})
                logger.tool_result(
                    "edit_slide_plan", f"applied={ok_n} failed={len(bad)}",
                    "\n".join(f"[{r.op}] {r.target}: " + ("ok" if r.ok else f"FAILED — {r.error}") for r in op_results),
                )
                # Echo current titles for the agent's next pass.
                observation: Dict[str, Any] = {
                    "applied": ok_n,
                    "failed": [
                        {"op": r.op, "target": r.target, "error": r.error}
                        for r in bad
                    ],
                }
                if bad:
                    new_plan = get_slide_plan(working) or {}
                    observation["current_titles"] = [
                        t
                        for s in (new_plan.get("slides") or [])
                        for t in (
                            [c.get("subsection", "")
                             for c in (s.get("columns") or [])]
                            if str(s.get("template_id", "")).startswith("T14")
                            else [s.get("subsection", "")]
                        )
                    ]
                pending_outputs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(observation, ensure_ascii=False),
                })
                continue

            if name == "ask_user":
                question = (fn_args.get("question") or "").strip()
                if not question:
                    answer = "(empty question; please proceed with best judgment)"
                    logger.tool_call("ask_user", {"question": "(empty)"})
                    logger.tool_result("ask_user", "empty")
                elif speak_count >= max_speak:
                    answer = (
                        "Speak budget exhausted. Use your best judgment "
                        "and call finish."
                    )
                    logger.tool_call("ask_user", {"question": question})
                    logger.tool_result("ask_user", "rejected (over budget)")
                else:
                    speak_count += 1
                    logger.tool_call("ask_user", {"question": question})
                    _spk_start = time.time()
                    answer, ans_in, ans_out = ask_user_handler(question)
                    _spk_dt = time.time() - _spk_start
                    in_tok_total += ans_in
                    out_tok_total += ans_out
                    speak_in += ans_in
                    speak_out += ans_out
                    speak_time += _spk_dt
                    logger.tool_result(
                        "ask_user", "ok",
                        f"question: {question}\nanswer: {answer}\n"
                        f"answer_tokens: in={ans_in} out={ans_out}",
                    )
                pending_outputs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"answer": answer}, ensure_ascii=False),
                })
                continue

            if name == "finish":
                finished = True
                logger.tool_call("finish", {})
                logger.tool_result("finish", "ok")
                pending_outputs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"ok": True}),
                })
                continue

            logger.tool_call(name, {"raw": str(fn_args)[:200]})
            logger.tool_result(name, "rejected (unknown tool)")
            pending_outputs.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"error": f"unknown tool {name}"}),
            })

        if finished:
            break
        if not pending_outputs:
            logger.event("js_respact_warn", f"turn {turn_idx}: no function call; nudging")
            next_input = (
                "You did not call any function. Apply your edits via "
                "edit_slide_plan / set_slide_override / patch_js, then call "
                "finish(). Any JS written as plain message text is ignored."
            )
            continue
        next_input = pending_outputs

    dt = time.time() - start
    speak_part = {"in": speak_in, "out": speak_out, "time": speak_time}
    if not finished:
        logger.event(
            "js_respact_warn",
            f"reached max_tool_iters={max_tool_iters} without finish; "
            "keeping the edits applied so far",
        )
    return working, in_tok_total, out_tok_total, dt, speak_part


# ── Public API ─────────────────────────────────────────────────────────────

def _resolve_js_path(args, js_path: Optional[str]) -> Path:
    if js_path:
        return Path(js_path)
    theme_id = getattr(args, "js_theme", 0)
    design_id = getattr(args, "js_design", 0)
    return Path(
        f"contents/{args.paper_name}/"
        f"{args.model_name_t}_{args.model_name_v}_pptxgenjs_"
        f"theme{theme_id}_design{design_id}.js"
    )


def _resolve_pptx_for_js(js_path: Path, args) -> Path:
    """Derive the output PPTX path from the JS filename pattern."""
    name = js_path.name
    # pattern: {model_t}_{model_v}_pptxgenjs_theme{T}_design{D}.js
    m = re.search(r"theme(\d+)_design(\d+)\.js$", name)
    theme_id = int(m.group(1)) if m else getattr(args, "js_theme", 0)
    design_id = int(m.group(2)) if m else getattr(args, "js_design", 0)
    return _default_pptx_path(args, theme_id, design_id)


def _do_round(
    args,
    js_path: Path,
    pptx_path: Path,
    raw_content: Any,
    feedback: str,
    logger: _FeedbackLogger,
    ask_user_handler: AskUserHandler,
    image_registry: Optional[set],
) -> Tuple[int, int, Dict[str, float], bool]:
    """Run one round: reviser produces new JS, persist, run node, snapshot.

    Returns ``(in_tok, out_tok, speak_part, node_ok)``.
    """
    use_gpt5 = "gpt-5" in getattr(args, "model_name_t", "").lower()
    if not use_gpt5:
        logger.event(
            "js_respact_warn",
            "non-gpt-5 backend; JS feedback currently requires GPT-5. Skipping.",
        )
        return 0, 0, {"in": 0, "out": 0, "time": 0.0}, False

    js_text = js_path.read_text(encoding="utf-8")
    prev_text = js_text  # rollback target on node failure
    new_js, in_tok, out_tok, _dt, speak_part = _revise_once_respact_gpt5(
        args, js_text, raw_content, feedback, logger,
        ask_user_handler=ask_user_handler,
        image_registry=image_registry,
    )
    if new_js == js_text:
        logger.event("js_respact", "no JS changes were made this round")
        return in_tok, out_tok, speak_part, True

    js_path.write_text(new_js, encoding="utf-8")
    logger.event("js_saved", str(js_path))

    ok, output = _run_node(js_path, logger)
    if not ok:
        logger.event(
            "node_rollback",
            "Node failed on revised JS; rolling back to previous version. "
            f"Error head: {output[:300]}",
        )
        js_path.write_text(prev_text, encoding="utf-8")
        # Re-run node on the rolled-back version to keep pptx in sync.
        _run_node(js_path, logger)
        return in_tok, out_tok, speak_part, False
    return in_tok, out_tok, speak_part, True


def apply_user_feedback_js(
    args, js_path: Optional[str] = None,
) -> Tuple[int, int, float, Dict[str, Dict[str, float]]]:
    """Interactive JS-level feedback loop.

    Returns ``(in, out, time, breakdown)``. Human path has no LLM reviewer,
    so only ``feedback_reviser`` is emitted.
    """
    js_p = _resolve_js_path(args, js_path)
    if not js_p.is_file():
        raise FileNotFoundError(f"JS file not found: {js_p}")
    pptx_p = _resolve_pptx_for_js(js_p, args)
    raw_content = _load_raw_content(args)
    image_registry = _load_image_registry(args)

    total_in = total_out = 0
    reviser_in = reviser_out = 0
    reviser_time = 0.0
    start = time.time()

    log_p = _log_path(args)
    logger = _FeedbackLogger(log_p)
    print(f"[feedback js] Session log → {log_p}")
    print(f"[feedback js] JS file:       {js_p}")
    print(f"[feedback js] JS pptx:       {pptx_p}  (exists={pptx_p.is_file()})")
    print(f"[feedback js] Snapshot root: {_snapshot_root(args)}")

    shutil.rmtree(_snapshot_root(args), ignore_errors=True)
    _repair_js_latex_inplace(js_p, logger)
    _snapshot_round(args, 0, js_p, pptx_p, logger)

    logger.section("INITIAL JS DECK (snapshot round 0 archived)")

    round_num = 0
    while True:
        print("\n" + "=" * 70)
        print(f"JS DECK REVIEW (round {round_num})")
        print("=" * 70)
        feedback = input(
            "\nEnter JS feedback (or press Enter / type 'ok' to approve): "
        ).strip()
        if feedback.lower() in ("", "ok", "approve", "done", "yes", "y"):
            logger.section(f"SESSION END — approved after {round_num} round(s)")
            log_user_feedback(
                stage="js_feedback_approval",
                content=feedback or "(empty)",
                metadata={"rounds": round_num},
            )
            print("[feedback js] Deck approved.")
            break

        round_num += 1
        logger.section(f"ROUND {round_num}")
        logger.event("user_input", feedback)
        log_user_feedback(
            stage="js_feedback_input",
            content=feedback,
            metadata={"round": round_num, "paper": args.paper_name},
        )

        _t0 = time.time()
        in_t, out_t, speak_part, node_ok = _do_round(
            args, js_p, pptx_p, raw_content, feedback, logger,
            _stdin_ask_user_handler, image_registry,
        )
        _dt = time.time() - _t0
        total_in += in_t
        total_out += out_t
        # Human path: ask_user is stdin (0 tokens); whole revision time is
        # reviser time minus stdin wait captured in speak_part.
        reviser_in += in_t - speak_part["in"]
        reviser_out += out_t - speak_part["out"]
        reviser_time += max(0.0, _dt - speak_part["time"])

        _snapshot_round(args, round_num, js_p, pptx_p, logger)
        print(f"[feedback js] Round {round_num} complete (node_ok={node_ok}).")

    total_time = time.time() - start
    logger.summary(total_in, total_out, total_time)
    print(f"[feedback js] Total: tokens in={total_in} out={total_out}; time={total_time:.2f}s")
    breakdown = {
        "js_feedback_reviser": {
            "in": reviser_in, "out": reviser_out, "time": reviser_time,
        },
    }
    return total_in, total_out, total_time, breakdown


def apply_simulated_feedback_js(
    args, js_path: Optional[str] = None,
) -> Tuple[int, int, float, Dict[str, Dict[str, float]]]:
    """LLM-simulated counterpart of ``apply_user_feedback_js``."""
    from slide_generation.content_generation.llm_feedback_simulator import (
        simulate_js_feedback,
        MAX_SIMULATED_JS_ROUNDS as MAX_SIMULATED_ROUNDS,
    )

    js_p = _resolve_js_path(args, js_path)
    if not js_p.is_file():
        raise FileNotFoundError(f"JS file not found: {js_p}")
    pptx_p = _resolve_pptx_for_js(js_p, args)
    raw_content = _load_raw_content(args)
    image_registry = _load_image_registry(args)

    total_in = total_out = 0
    reviewer_in = reviewer_out = 0
    reviewer_time = 0.0
    reviser_in = reviser_out = 0
    reviser_time = 0.0
    start = time.time()

    log_p = _log_path(args)
    logger = _FeedbackLogger(log_p)
    print(f"[feedback js] Simulated session log → {log_p}")
    print(f"[feedback js] JS file:       {js_p}")
    print(f"[feedback js] JS pptx:       {pptx_p}  (exists={pptx_p.is_file()})")
    print(f"[feedback js] Snapshot root: {_snapshot_root(args)}")

    shutil.rmtree(_snapshot_root(args), ignore_errors=True)
    _repair_js_latex_inplace(js_p, logger)
    _snapshot_round(args, 0, js_p, pptx_p, logger)

    reviewer_prior: List[str] = []

    for sim_round in range(1, MAX_SIMULATED_ROUNDS + 1):
        logger.section(f"SIMULATED JS REVIEW ROUND {sim_round}")
        print(f"\n[feedback js] Simulated review round {sim_round}/{MAX_SIMULATED_ROUNDS}...")

        plan = get_slide_plan(js_p.read_text(encoding="utf-8")) or {}
        feedback, in_t, out_t, dt = simulate_js_feedback(
            str(pptx_p), plan, raw_content, args,
            round_num=sim_round, prior_feedback=reviewer_prior,
        )
        total_in += in_t
        total_out += out_t
        reviewer_in += in_t
        reviewer_out += out_t
        reviewer_time += dt
        logger.event("vlm_review", f"tokens in={in_t} out={out_t}  time={dt:.2f}s")

        if feedback is None:
            logger.event("vlm_review", "approved")
            print("[feedback js] Deck approved by simulated reviewer.")
            log_user_feedback(
                stage="js_feedback_approval",
                content="(llm-simulated approval)",
                metadata={"rounds": sim_round - 1, "source": "llm_simulated"},
            )
            break

        logger.block("vlm_feedback", f"round={sim_round}", feedback)
        reviewer_prior.append(feedback)
        log_user_feedback(
            stage="js_feedback_input",
            content=feedback,
            metadata={"round": sim_round, "paper": args.paper_name,
                      "source": "llm_simulated"},
        )

        js_text_for_handler = js_p.read_text(encoding="utf-8")
        sim_handler = _make_simulated_ask_user_handler(
            args, original_feedback=feedback,
            js_text=js_text_for_handler, raw_content=raw_content,
        )
        _t0 = time.time()
        in_t, out_t, speak_part, node_ok = _do_round(
            args, js_p, pptx_p, raw_content, feedback, logger,
            sim_handler, image_registry,
        )
        _dt = time.time() - _t0
        total_in += in_t
        total_out += out_t
        reviewer_in += speak_part["in"]
        reviewer_out += speak_part["out"]
        reviewer_time += speak_part["time"]
        reviser_in += in_t - speak_part["in"]
        reviser_out += out_t - speak_part["out"]
        reviser_time += max(0.0, _dt - speak_part["time"])

        _snapshot_round(args, sim_round, js_p, pptx_p, logger)
    else:
        print(f"[feedback js] Reached max simulated rounds ({MAX_SIMULATED_ROUNDS}).")
        logger.event("vlm_review", f"max rounds reached ({MAX_SIMULATED_ROUNDS})")

    # Persist reviewer's feedback history.
    try:
        prefix = f"<{args.model_name_t}_{args.model_name_v}>"
        hist_path = Path(
            f"contents/{args.paper_name}/{prefix}_js_feedback_history.json"
        )
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        hist_path.write_text(
            json.dumps(reviewer_prior, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.event("feedback_history",
                     f"wrote {len(reviewer_prior)} item(s) → {hist_path}")
    except Exception as exc:
        logger.event("js_respact_warn", f"could not write feedback history: {exc}")

    total_time = time.time() - start
    logger.summary(total_in, total_out, total_time)
    print(f"[feedback js] Simulated total: tokens in={total_in} out={total_out}; time={total_time:.2f}s")
    breakdown = {
        "js_feedback_reviewer": {
            "in": reviewer_in, "out": reviewer_out, "time": reviewer_time,
        },
        "js_feedback_reviser": {
            "in": reviser_in, "out": reviser_out, "time": reviser_time,
        },
    }
    return total_in, total_out, total_time, breakdown
