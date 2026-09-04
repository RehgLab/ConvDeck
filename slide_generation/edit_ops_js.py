"""
JS-stage edit-op engines for the JS feedback reviser.

Three engines, all operating on the *generated* PptxGenJS .js file as a
string:

  • apply_js_patches(js_text, edits)        — free-form find/replace
  • apply_slide_overrides(js_text, overrides) — structured per-slide overrides
                                                written into SLIDE_OVERRIDES
  • apply_slide_plan_edits(js_text, ops)    — parse SLIDE_PLAN literal, run
                                                edit_ops.apply_slide_plan_ops,
                                                splice JSON back in

Each returns ``(new_js_text, [OpResult, ...])``. The input string is not
mutated. OpResult mirrors edit_ops.OpResult shape for logging parity.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from slide_generation.edit_ops import (
    OpResult,
    apply_slide_plan_ops,
    repair_slide_plan_latex,
)


# ── String-patch engine ─────────────────────────────────────────────────────

def apply_js_patches(
    js_text: str,
    edits: List[Dict[str, Any]],
) -> Tuple[str, List[OpResult]]:
    """Apply a batch of find/replace edits to ``js_text``.

    Each edit is ``{"find": str, "replace": str}``. ``find`` must match
    exactly once in the current working text. Edits are applied in order;
    a failing edit is skipped and the rest still apply.
    """
    results: List[OpResult] = []
    work = js_text
    for i, ed in enumerate(edits or []):
        find = (ed or {}).get("find", "")
        repl = (ed or {}).get("replace", "")
        target = (find or "")[:80].replace("\n", " ")
        res = OpResult(index=i, op="patch_js", target=target)
        if not isinstance(find, str) or not find:
            res.ok = False
            res.error = "find must be a non-empty string"
            results.append(res)
            continue
        if not isinstance(repl, str):
            res.ok = False
            res.error = "replace must be a string"
            results.append(res)
            continue
        count = work.count(find)
        if count == 0:
            res.ok = False
            res.error = "find string not present in current JS"
            results.append(res)
            continue
        if count > 1:
            res.ok = False
            res.error = (
                f"find string is not unique ({count} matches); "
                "include more surrounding context"
            )
            results.append(res)
            continue
        work = work.replace(find, repl, 1)
        results.append(res)
    return work, results


# ── SLIDE_OVERRIDES engine ──────────────────────────────────────────────────

_OVR_BLOCK_RE = re.compile(
    r"(const\s+SLIDE_OVERRIDES\s*=\s*)(\{[\s\S]*?\})(\s*;)",
)


_VALID_OVERRIDE_KEYS = {
    "bullet_font_size", "title_font_size",
    "body_xywh", "image_xywh", "hide_figure",
}


def _parse_slide_overrides(js_text: str) -> Tuple[Dict[str, Any], Optional[re.Match]]:
    m = _OVR_BLOCK_RE.search(js_text)
    if not m:
        return {}, None
    try:
        existing = json.loads(m.group(2))
    except Exception:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    return existing, m


def _validate_override(ov: Dict[str, Any]) -> Optional[str]:
    if not isinstance(ov, dict):
        return "override must be an object"
    for k in ov.keys():
        if k not in _VALID_OVERRIDE_KEYS:
            return f"unknown override key {k!r}; valid: {sorted(_VALID_OVERRIDE_KEYS)}"
    for k in ("bullet_font_size", "title_font_size"):
        if k in ov and not isinstance(ov[k], (int, float)):
            return f"{k} must be a number"
    if "hide_figure" in ov and not isinstance(ov["hide_figure"], bool):
        return "hide_figure must be a boolean"
    if "body_xywh" in ov:
        v = ov["body_xywh"]
        if not (isinstance(v, list) and len(v) == 4 and all(isinstance(x, (int, float)) for x in v)):
            return "body_xywh must be a list of 4 numbers [x, y, w, h]"
    if "image_xywh" in ov:
        v = ov["image_xywh"]
        if not isinstance(v, list):
            return "image_xywh must be a list of [x, y, w, h] (or null) per visual"
        for r in v:
            if r is None:
                continue
            if not (isinstance(r, list) and len(r) == 4 and all(isinstance(x, (int, float)) for x in r)):
                return "each image_xywh entry must be null or [x, y, w, h]"
    return None


def apply_slide_overrides(
    js_text: str,
    overrides: List[Dict[str, Any]],
    n_slides: Optional[int] = None,
) -> Tuple[str, List[OpResult]]:
    """Merge per-slide overrides into the SLIDE_OVERRIDES object in ``js_text``.

    Each ``overrides`` item is:
      ``{"slide_index": <1-based int>, "override": {...}}``

    Unknown keys are rejected. Existing keys for that slide are merged (a
    new entry's keys overwrite the prior values; other keys are preserved).
    Pass ``override={}`` to clear a slide's overrides entirely.
    """
    results: List[OpResult] = []
    existing, m = _parse_slide_overrides(js_text)
    if m is None:
        for i in range(len(overrides or [])):
            res = OpResult(index=i, op="set_slide_override", target="(no SLIDE_OVERRIDES block)")
            res.ok = False
            res.error = (
                "SLIDE_OVERRIDES block not found in JS. Was the file generated "
                "by an up-to-date generate_pptx_from_plan_using_pptxgenjs?"
            )
            results.append(res)
        return js_text, results

    updated = dict(existing)
    for i, item in enumerate(overrides or []):
        slide_idx = (item or {}).get("slide_index")
        ov = (item or {}).get("override")
        target = f"slide {slide_idx}"
        res = OpResult(index=i, op="set_slide_override", target=target)
        if not isinstance(slide_idx, int) or slide_idx < 1:
            res.ok = False
            res.error = "slide_index must be a 1-based positive integer"
            results.append(res)
            continue
        if n_slides is not None and slide_idx > n_slides:
            res.ok = False
            res.error = f"slide_index {slide_idx} out of range (1..{n_slides})"
            results.append(res)
            continue
        err = _validate_override(ov or {})
        if err is not None:
            res.ok = False
            res.error = err
            results.append(res)
            continue
        key = str(slide_idx)
        if not ov:
            updated.pop(key, None)
        else:
            merged = dict(updated.get(key) or {})
            merged.update(ov)
            updated[key] = merged
        results.append(res)

    new_block = json.dumps(updated, indent=2, ensure_ascii=False)
    new_js = js_text[:m.start(2)] + new_block + js_text[m.end(2):]
    return new_js, results


# ── SLIDE_PLAN-in-JS engine ─────────────────────────────────────────────────

def _find_slide_plan_block(js_text: str) -> Optional[Tuple[int, int, Dict[str, Any]]]:
    """Locate ``const SLIDE_PLAN = { ... };`` and parse the object literal.

    Returns ``(start_obj, end_obj, parsed)`` where the slice
    ``js_text[start_obj:end_obj]`` is the JSON-compatible object literal.
    Returns None if not found / not parseable.
    """
    needle = "const SLIDE_PLAN = "
    i = js_text.find(needle)
    if i < 0:
        return None
    j = i + len(needle)
    if j >= len(js_text) or js_text[j] != "{":
        return None
    depth = 0
    in_str = False
    str_ch = ""
    esc = False
    k = j
    while k < len(js_text):
        c = js_text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == str_ch:
                in_str = False
        else:
            if c in ('"', "'"):
                in_str = True
                str_ch = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end_obj = k + 1
                    raw = js_text[j:end_obj]
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        return None
                    return (j, end_obj, parsed)
        k += 1
    return None


def apply_slide_plan_edits(
    js_text: str,
    ops: List[Dict[str, Any]],
    image_registry: Optional[set] = None,
) -> Tuple[str, List[OpResult], Optional[Dict[str, Any]]]:
    """Parse SLIDE_PLAN out of the JS, run slide-plan ops, splice back.

    Returns ``(new_js_text, op_results, new_plan_or_None)``. If the
    SLIDE_PLAN block can't be located/parsed, every op fails with a single
    diagnostic and the JS is returned unchanged.
    """
    located = _find_slide_plan_block(js_text)
    if located is None:
        results: List[OpResult] = []
        for i in range(len(ops or [])):
            res = OpResult(index=i, op=(ops[i] or {}).get("op", ""))
            res.ok = False
            res.error = "could not locate or parse SLIDE_PLAN object in JS"
            results.append(res)
        return js_text, results, None
    start_obj, end_obj, plan = located
    new_plan, op_results = apply_slide_plan_ops(plan, ops, image_registry)
    new_block = json.dumps(new_plan, indent=2, ensure_ascii=False)
    new_js = js_text[:start_obj] + new_block + js_text[end_obj:]
    return new_js, op_results, new_plan


def repair_js_latex(js_text: str) -> Tuple[str, bool]:
    """Locate SLIDE_PLAN inside the JS, repair eaten-backslash LaTeX commands
    in every bullet/sub-bullet/paragraph, and splice back. Returns
    ``(new_js_text, changed)``. If SLIDE_PLAN can't be located, returns the
    input unchanged."""
    located = _find_slide_plan_block(js_text)
    if located is None:
        return js_text, False
    start_obj, end_obj, plan = located
    changed = repair_slide_plan_latex(plan)
    if not changed:
        return js_text, False
    new_block = json.dumps(plan, indent=2, ensure_ascii=False)
    return js_text[:start_obj] + new_block + js_text[end_obj:], True


def get_slide_plan(js_text: str) -> Optional[Dict[str, Any]]:
    """Return a copy of the SLIDE_PLAN dict embedded in ``js_text`` or None."""
    located = _find_slide_plan_block(js_text)
    return located[2] if located else None
