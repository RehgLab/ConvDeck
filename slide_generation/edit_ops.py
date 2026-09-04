"""
Localized edit-operation engine for the feedback revisers.

Instead of regenerating the whole ``slide_plan.json`` / ``raw_content_rst.json``
each feedback round, the ReSpAct reviser emits a small list of typed *edit
operations* and this module applies them in pure Python.

Two public entry points:

  • ``apply_slide_plan_ops(plan, ops, image_registry=None)``  — content stage
  • ``apply_outline_ops(slides, ops)``                        — outline stage
"""

from __future__ import annotations

import copy
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Per-slide bullet caps — soft (emit a warning, not a failure).
_BULLET_CAP_FIGURE = 4
_BULLET_CAP_TEXTONLY = 6
_FUZZY_CUTOFF = 0.85


# ── Result type ─────────────────────────────────────────────────────────────

@dataclass
class OpResult:
    """Outcome of one edit operation.

    ``ok`` reports whether the op applied. ``warnings`` collects non-fatal
    issues (e.g. exceeding a bullet cap) — the op still applied. The agent
    sees ``error`` / ``warnings`` as its tool observation and can retry.
    """

    index: int                       # position of the op in the batch
    op: str                          # op type, e.g. "set_bullets"
    target: str = ""                 # human-readable target description
    ok: bool = True
    error: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"op": self.op, "target": self.target, "ok": self.ok}
        if self.error:
            d["error"] = self.error
        if self.warnings:
            d["warnings"] = self.warnings
        return d


class _OpError(Exception):
    """Raised inside an op handler to fail that op cleanly."""


# ── Bullet helpers ──────────────────────────────────────────────────────────

# Restore leading backslash on common LaTeX commands whose `\` was eaten by
# JSON escape handling at the function-call boundary. Two flavors:
#   * `\textcolor` → JSON parses `\t` as TAB and consumes the `t`, leaving
#     `<TAB>extcolor` (or just `extcolor` if the TAB was later stripped).
#     Same for `\textbf` → `extbf`, `\textit` → `extit`.
#   * `\emph` / `\underline` → backslash dropped, command name intact.
# Optionally absorbs a leading stray control char (\t \n \r \b \f) left over
# from the eaten escape. Idempotent — `(?<!\\)` skips already-repaired text.
# Truncated forms (extcolor / extbf / extit): the leading `t` was consumed by
# `\t` JSON escape. Must NOT match when preceded by `t` (that would be the
# already-correct `\textcolor`). Optionally absorbs a stray leading TAB.
# Full forms: backslash dropped but command name intact; must not be already
# preceded by `\`.
_LATEX_REPAIR_RE = re.compile(
    r"(?:"
    r"(?<!t)\t?(?P<trunc>extcolor|extbf|extit)"
    r"|"
    r"(?<!\\)(?P<full>textcolor|textbf|textit|emph|underline)"
    r")\{"
)


def _repair_latex(text: str) -> str:
    if not isinstance(text, str) or "{" not in text:
        return text
    def _sub(m: "re.Match[str]") -> str:
        cmd = m.group("trunc")
        if cmd is not None:
            cmd = "t" + cmd  # extcolor → textcolor
        else:
            cmd = m.group("full")
        return "\\" + cmd + "{"
    return _LATEX_REPAIR_RE.sub(_sub, text)


def _norm_bullet(b: Any) -> Dict[str, Any]:
    """Coerce one bullet into the canonical ``{"text", "sub"}`` shape."""
    if isinstance(b, str):
        return {"text": _repair_latex(b), "sub": []}
    if isinstance(b, dict):
        text = b.get("text", "")
        sub = b.get("sub", []) or []
        if not isinstance(sub, list):
            sub = []
        return {"text": _repair_latex(str(text)), "sub": [_norm_bullet(s) for s in sub]}
    return {"text": _repair_latex(str(b)), "sub": []}


def repair_slide_plan_latex(plan: Dict[str, Any]) -> bool:
    """Walk a slide_plan dict and repair eaten-backslash LaTeX in every bullet,
    sub-bullet, and paragraph (incl. T14 columns). Mutates in place. Returns
    True if anything changed."""
    changed = False

    def _fix_str(s: Any) -> Any:
        nonlocal changed
        if isinstance(s, str):
            r = _repair_latex(s)
            if r != s:
                changed = True
            return r
        return s

    def _fix_bullets(bullets: Any) -> None:
        if not isinstance(bullets, list):
            return
        for b in bullets:
            if isinstance(b, dict):
                if "text" in b:
                    b["text"] = _fix_str(b["text"])
                _fix_bullets(b.get("sub"))

    def _fix_container(c: Dict[str, Any]) -> None:
        if "paragraph" in c and isinstance(c["paragraph"], str):
            c["paragraph"] = _fix_str(c["paragraph"])
        _fix_bullets(c.get("bullets"))

    for slide in plan.get("slides", []) or []:
        if not isinstance(slide, dict):
            continue
        if str(slide.get("template_id", "")).startswith("T14"):
            for col in slide.get("columns", []) or []:
                if isinstance(col, dict):
                    _fix_container(col)
        else:
            _fix_container(slide)
    return changed


def _norm_bullets(items: Any) -> List[Dict[str, Any]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise _OpError("'bullets' must be a list")
    return [_norm_bullet(b) for b in items]


# ── Content-stage slide identity ────────────────────────────────────────────

def _is_t14(slide: Dict[str, Any]) -> bool:
    return str(slide.get("template_id", "")).startswith("T14")


def _slide_titles(slide: Dict[str, Any]) -> List[str]:
    """Every title a slide can be addressed by."""
    if _is_t14(slide):
        return [str(c.get("subsection", "")) for c in slide.get("columns", []) or []]
    return [str(slide.get("subsection", ""))]


@dataclass
class _Match:
    """Resolution result for a content-stage slide reference."""
    slide: Dict[str, Any]
    column_index: Optional[int] = None   # set when the title named a T14 column
    fuzzy: bool = False


def _build_title_map(slides: List[Dict[str, Any]]) -> Dict[str, _Match]:
    """Map every addressable title to its slide (snapshot taken before any op runs)."""
    m: Dict[str, _Match] = {}
    for slide in slides:
        if _is_t14(slide):
            for ci, col in enumerate(slide.get("columns", []) or []):
                title = str(col.get("subsection", ""))
                if title:
                    m.setdefault(title, _Match(slide, column_index=ci))
        else:
            title = str(slide.get("subsection", ""))
            if title:
                m.setdefault(title, _Match(slide))
    return m


def _resolve_slide(
    title: str,
    title_map: Dict[str, _Match],
    live_slides: List[Dict[str, Any]],
) -> _Match:
    """Resolve a slide reference: snapshot map → live scan → fuzzy match."""
    if not title:
        raise _OpError("slide reference is empty")
    # 1. exact, against the pre-batch snapshot map (immune to earlier retitles)
    hit = title_map.get(title)
    if hit is not None and _contains(live_slides, hit.slide):
        return hit
    # 2. exact, against slides added during this batch
    for slide in live_slides:
        if _is_t14(slide):
            for ci, col in enumerate(slide.get("columns", []) or []):
                if str(col.get("subsection", "")) == title:
                    return _Match(slide, column_index=ci)
        elif str(slide.get("subsection", "")) == title:
            return _Match(slide)
    # 3. fuzzy fallback
    all_titles = [t for s in live_slides for t in _slide_titles(s) if t]
    close = difflib.get_close_matches(title, all_titles, n=1, cutoff=_FUZZY_CUTOFF)
    if close:
        m = _resolve_slide(close[0], {}, live_slides)
        m.fuzzy = True
        return m
    raise _OpError(
        f"no slide titled {title!r} (and no close match) — "
        f"check the exact subsection title. Available titles: {all_titles}"
    )


def _contains(slides: List[Dict[str, Any]], slide: Dict[str, Any]) -> bool:
    """Membership by object identity (dict ``in`` would match by value)."""
    return any(s is slide for s in slides)


def _idx_of(slides: List[Dict[str, Any]], slide: Dict[str, Any]) -> int:
    """Position by object identity, or raise if the slide is gone."""
    for i, s in enumerate(slides):
        if s is slide:
            return i
    raise _OpError("slide was removed by an earlier op in this batch")


def _bullet_cap(slide: Dict[str, Any]) -> int:
    tid = str(slide.get("template_id", ""))
    return _BULLET_CAP_TEXTONLY if tid == "T1_TextOnly" else _BULLET_CAP_FIGURE


# ── Content-stage op handlers ───────────────────────────────────────────────
# Each handler receives (plan, op, ctx) and mutates `plan` in place, or raises
# _OpError. `ctx` carries the title_map and image_registry. Handlers append
# warnings to the supplied `warnings` list.

def _set_field_target(m: _Match, op: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[int]]:
    """Return (container, column_index) to write bullets/paragraph into.

    For a T14 slide the container is the column dict; for a regular slide it
    is the slide itself.
    """
    slide = m.slide
    if _is_t14(slide):
        ci = m.column_index
        if ci is None:
            # title named the slide but not a column — require explicit column
            col_ref = op.get("column")
            if col_ref is None:
                raise _OpError("T14 slide needs a 'column' (index or subsection title)")
            cols = slide.get("columns", []) or []
            if isinstance(col_ref, int):
                if not 0 <= col_ref < len(cols):
                    raise _OpError(f"column index {col_ref} out of range")
                ci = col_ref
            else:
                ci = next(
                    (i for i, c in enumerate(cols)
                     if str(c.get("subsection", "")) == str(col_ref)),
                    None,
                )
                if ci is None:
                    raise _OpError(f"no column titled {col_ref!r}")
        return slide["columns"][ci], ci
    return slide, None


def _op_set_bullets(plan, op, ctx, warnings):
    m = _resolve_slide(op.get("slide_title", ""), ctx["map"], plan["slides"])
    container, _ = _set_field_target(m, op)
    bullets = _norm_bullets(op.get("bullets"))
    if not bullets:
        raise _OpError("'bullets' is empty — use remove_slide to drop a slide")
    container["bullets"] = bullets
    # T14 rule: a column has EITHER bullets OR paragraph.
    if "paragraph" in container:
        container["paragraph"] = ""
    cap = _bullet_cap(m.slide)
    if len(bullets) > cap:
        warnings.append(f"{len(bullets)} bullets exceeds cap {cap} for this template")
    if m.fuzzy:
        warnings.append(f"resolved by fuzzy match to {_slide_titles(m.slide)}")


def _op_set_paragraph(plan, op, ctx, warnings):
    m = _resolve_slide(op.get("slide_title", ""), ctx["map"], plan["slides"])
    container, _ = _set_field_target(m, op)
    container["paragraph"] = _repair_latex(str(op.get("paragraph", "")))
    if "bullets" in container:
        container["bullets"] = []
    if m.fuzzy:
        warnings.append(f"resolved by fuzzy match to {_slide_titles(m.slide)}")


def _op_set_template(plan, op, ctx, warnings):
    m = _resolve_slide(op.get("slide_title", ""), ctx["map"], plan["slides"])
    new_tid = str(op.get("template_id", ""))
    if not new_tid:
        raise _OpError("'template_id' is required")
    old_tid = str(m.slide.get("template_id", ""))
    if old_tid.startswith("T14") != new_tid.startswith("T14"):
        raise _OpError(
            "cannot switch a slide into or out of a T14 (two-column) template "
            "via set_template — structure differs"
        )
    has_fig = bool(m.slide.get("images")) or bool(m.slide.get("tables"))
    if not new_tid.startswith("T1") and not has_fig and not new_tid.startswith("T14"):
        warnings.append(f"{new_tid} expects a figure but slide has none")
    m.slide["template_id"] = new_tid


def _op_remove_figure(plan, op, ctx, warnings):
    m = _resolve_slide(op.get("slide_title", ""), ctx["map"], plan["slides"])
    if _is_t14(m.slide):
        raise _OpError("T14 slides carry no figures")
    m.slide["images"] = []
    m.slide["tables"] = []
    # Deterministic rule: a figure-less slide must use a text-only template.
    if str(m.slide.get("template_id", "")) != "T1_TextOnly":
        m.slide["template_id"] = "T1_TextOnly"


def _op_retitle(plan, op, ctx, warnings):
    m = _resolve_slide(op.get("slide_title", ""), ctx["map"], plan["slides"])
    new_title = str(op.get("new_title", "")).strip()
    if not new_title:
        raise _OpError("'new_title' is required")
    if _is_t14(m.slide):
        if m.column_index is None:
            raise _OpError("retitling a T14 slide must target a column")
        m.slide["columns"][m.column_index]["subsection"] = new_title
    else:
        m.slide["subsection"] = new_title


def _normalize_new_slide(raw: Any, image_registry: Optional[set]) -> Dict[str, Any]:
    """Validate + normalize a slide object supplied by add_slide."""
    if not isinstance(raw, dict):
        raise _OpError("'slide' must be an object")
    if str(raw.get("template_id", "")).startswith("T14"):
        # Two-column slides are never created fresh by feedback; reject.
        raise _OpError("add_slide cannot create a T14 two-column slide")
    slide = {
        "section": str(raw.get("section", "")),
        "subsection": str(raw.get("subsection", "")).strip(),
        "template_id": str(raw.get("template_id", "T1_TextOnly")) or "T1_TextOnly",
        "bullets": _norm_bullets(raw.get("bullets")),
        "paragraph": str(raw.get("paragraph", "")),
        "images": list(raw.get("images") or []),
        "tables": list(raw.get("tables") or []),
        "reference": str(raw.get("reference", "")),
    }
    if not slide["subsection"]:
        raise _OpError("new slide needs a non-empty 'subsection'")
    if image_registry is not None:
        for fn in slide["images"] + slide["tables"]:
            if Path(str(fn)).name not in image_registry:
                raise _OpError(f"figure {fn!r} is not a registered asset")
    return slide


def _op_add_slide(plan, op, ctx, warnings):
    slide = _normalize_new_slide(op.get("slide"), ctx["image_registry"])
    slides = plan["slides"]
    if op.get("after_title"):
        m = _resolve_slide(op["after_title"], ctx["map"], slides)
        pos = _idx_of(slides, m.slide) + 1
    elif op.get("before_title"):
        m = _resolve_slide(op["before_title"], ctx["map"], slides)
        pos = _idx_of(slides, m.slide)
    elif op.get("at_index") is not None:
        pos = max(0, min(int(op["at_index"]), len(slides)))
    else:
        pos = len(slides)
    slides.insert(pos, slide)


def _op_remove_slide(plan, op, ctx, warnings):
    m = _resolve_slide(op.get("slide_title", ""), ctx["map"], plan["slides"])
    del plan["slides"][_idx_of(plan["slides"], m.slide)]


def _op_move_slide(plan, op, ctx, warnings):
    """Relocate one slide. Cheaper and far less error-prone than a full
    reorder (no need to enumerate every slide)."""
    slides = plan["slides"]
    m = _resolve_slide(op.get("slide_title", ""), ctx["map"], slides)
    src = m.slide
    cur = _idx_of(slides, src)
    if op.get("after_title"):
        anchor = _resolve_slide(op["after_title"], ctx["map"], slides).slide
        if anchor is src:
            raise _OpError("cannot move a slide relative to itself")
        a_idx = _idx_of(slides, anchor)
        slides.pop(cur)
        if a_idx > cur:
            a_idx -= 1
        slides.insert(a_idx + 1, src)
    elif op.get("before_title"):
        anchor = _resolve_slide(op["before_title"], ctx["map"], slides).slide
        if anchor is src:
            raise _OpError("cannot move a slide relative to itself")
        a_idx = _idx_of(slides, anchor)
        slides.pop(cur)
        if a_idx > cur:
            a_idx -= 1
        slides.insert(a_idx, src)
    elif op.get("at_index") is not None:
        slides.pop(cur)
        pos = max(0, min(int(op["at_index"]), len(slides)))
        slides.insert(pos, src)
    else:
        raise _OpError(
            "move_slide needs 'after_title', 'before_title', or 'at_index'"
        )


def _op_reorder(plan, op, ctx, warnings):
    order = op.get("order")
    if not isinstance(order, list) or not order:
        raise _OpError("'order' must be a non-empty list of slide titles")
    slides = plan["slides"]
    picked: List[Dict[str, Any]] = []
    seen = set()
    for title in order:
        m = _resolve_slide(str(title), ctx["map"], slides)
        if id(m.slide) in seen:
            raise _OpError(f"slide {title!r} listed twice in 'order'")
        seen.add(id(m.slide))
        picked.append(m.slide)
    if len(picked) != len(slides):
        missing = [t for s in slides if id(s) not in seen for t in _slide_titles(s)]
        raise _OpError(
            f"'order' must list every slide exactly once; missing: {missing}"
        )
    plan["slides"] = picked


def _op_split_slide(plan, op, ctx, warnings):
    """Split one slide into >=2; figure/template/reference inherited by all."""
    m = _resolve_slide(op.get("slide_title", ""), ctx["map"], plan["slides"])
    src = m.slide
    if _is_t14(src):
        raise _OpError("splitting T14 two-column slides is not supported")
    halves = op.get("halves")
    if not isinstance(halves, list) or len(halves) < 2:
        raise _OpError("'halves' must list >=2 parts")
    new_slides: List[Dict[str, Any]] = []
    for h in halves:                       # build fully before mutating
        if not isinstance(h, dict):
            raise _OpError("each half must be an object")
        part = {
            "section": src.get("section", ""),
            "subsection": str(h.get("subsection", src.get("subsection", ""))).strip(),
            "template_id": src.get("template_id", "T1_TextOnly"),
            "bullets": _norm_bullets(h.get("bullets")),
            "paragraph": str(h.get("paragraph", "")),
            "images": list(src.get("images") or []),     # inherit figure
            "tables": list(src.get("tables") or []),
            "reference": src.get("reference", ""),        # inherit reference
        }
        if not part["subsection"]:
            raise _OpError("each split half needs a 'subsection'")
        new_slides.append(part)
    pos = _idx_of(plan["slides"], src)
    plan["slides"][pos:pos + 1] = new_slides


def _op_merge_slides(plan, op, ctx, warnings):
    """Merge >=2 slides into one; figures unioned, references concatenated."""
    titles = op.get("slide_titles")
    if not isinstance(titles, list) or len(titles) < 2:
        raise _OpError("'slide_titles' must list >=2 slides to merge")
    matches = [_resolve_slide(str(t), ctx["map"], plan["slides"]) for t in titles]
    sources = [m.slide for m in matches]
    if len({id(s) for s in sources}) != len(sources):
        raise _OpError("'slide_titles' resolved the same slide more than once")
    if any(_is_t14(s) for s in sources):
        raise _OpError("merging T14 two-column slides is not supported")
    imgs, tbls = [], []
    for s in sources:
        for x in s.get("images") or []:
            if x not in imgs:
                imgs.append(x)
        for x in s.get("tables") or []:
            if x not in tbls:
                tbls.append(x)
    refs = [s.get("reference", "") for s in sources if s.get("reference")]
    merged = {
        "section": sources[0].get("section", ""),
        "subsection": str(op.get("merged_subsection", sources[0].get("subsection", ""))).strip(),
        "template_id": sources[0].get("template_id", "T1_TextOnly"),
        "bullets": _norm_bullets(op.get("bullets")),
        "paragraph": str(op.get("paragraph", "")),
        "images": imgs,
        "tables": tbls,
        "reference": refs[0] if refs else "",
    }
    if not merged["bullets"] and not merged["paragraph"]:
        raise _OpError("merge needs 'bullets' or 'paragraph' for the merged slide")
    if not imgs and not tbls:
        merged["template_id"] = "T1_TextOnly"
    positions = sorted(_idx_of(plan["slides"], s) for s in sources)
    for idx in reversed(positions):           # remove all sources
        del plan["slides"][idx]
    plan["slides"].insert(positions[0], merged)


_CONTENT_OPS = {
    "set_bullets": _op_set_bullets,
    "set_paragraph": _op_set_paragraph,
    "set_template": _op_set_template,
    "remove_figure": _op_remove_figure,
    "retitle": _op_retitle,
    "add_slide": _op_add_slide,
    "remove_slide": _op_remove_slide,
    "move_slide": _op_move_slide,
    "reorder": _op_reorder,
    "split_slide": _op_split_slide,
    "merge_slides": _op_merge_slides,
}


def apply_slide_plan_ops(
    plan: Dict[str, Any],
    ops: List[Dict[str, Any]],
    image_registry: Optional[set] = None,
) -> Tuple[Dict[str, Any], List[OpResult]]:
    """Apply a batch of edit ops to a slide plan.

    ``plan`` is not mutated — a deep copy is edited and returned. ``ops`` is a
    list of ``{"op": <type>, ...}`` dicts. ``image_registry``, if given, is the
    set of registered asset *filenames* (``Path(...).name``); add_slide figures
    not in it are rejected.

    Returns ``(new_plan, results)``. Each op gets one OpResult; a failing op is
    skipped and the rest still apply (restructure ops fail atomically).
    """
    if not isinstance(plan, dict) or "slides" not in plan:
        raise ValueError("plan must be a dict with a 'slides' key")
    work = copy.deepcopy(plan)
    work.setdefault("metadata", plan.get("metadata", {}))
    ctx = {
        "map": _build_title_map(work["slides"]),   # snapshot — built once
        "image_registry": image_registry,
    }
    results: List[OpResult] = []
    for i, op in enumerate(ops or []):
        op_type = (op or {}).get("op", "")
        target = (
            op.get("slide_title")
            or op.get("after_title")
            or op.get("before_title")
            or ((op.get("slide") or {}).get("subsection") if isinstance(op.get("slide"), dict) else op.get("slide"))
            or (",".join(op.get("slide_titles", [])) if op.get("slide_titles") else "")
            or ""
        )
        res = OpResult(index=i, op=op_type, target=str(target))
        handler = _CONTENT_OPS.get(op_type)
        if handler is None:
            res.ok = False
            res.error = f"unknown op {op_type!r}; valid: {sorted(_CONTENT_OPS)}"
            results.append(res)
            continue
        before = copy.deepcopy(work["slides"])
        try:
            handler(work, op, ctx, res.warnings)
        except _OpError as exc:
            res.ok = False
            res.error = str(exc)
            work["slides"] = before          # roll back this op only
        except Exception as exc:             # pragma: no cover - defensive
            res.ok = False
            res.error = f"unexpected error: {exc}"
            work["slides"] = before
        results.append(res)
    return work, results


# ── Outline-stage ───────────────────────────────────────────────────────────

def _outline_titles(slides: List[Dict[str, Any]]) -> List[str]:
    return [str(s.get("title", "")) for s in slides]


def _as_ref(r: Any) -> Dict[str, Any]:
    """Coerce a bare title/index into a ``{title}`` / ``{index}`` ref dict."""
    if isinstance(r, dict):
        return r
    return {"index": r} if isinstance(r, int) else {"title": r}


def _outline_resolve(
    ref: Dict[str, Any],
    slides: List[Dict[str, Any]],
    ctx: Optional[Dict[str, Any]] = None,
) -> int:
    """Resolve an outline op's target to a 0-based index.

    An explicit ``index`` wins when present; otherwise resolves by ``title``.
    Title resolution order: exact title → rename map (a title an earlier op
    in this batch renamed away from) → fuzzy match. A genuinely unknown title
    fails with an error that lists the current titles so the agent can
    self-correct in one retry.
    """
    idx = ref.get("index")
    if idx is not None:
        idx = int(idx)
        if not 0 <= idx < len(slides):
            raise _OpError(f"index {idx} out of range (0..{len(slides) - 1})")
        return idx
    title = ref.get("title")
    if title is not None and str(title) != "":
        title = str(title)
        for i, s in enumerate(slides):
            if str(s.get("title", "")) == title:
                return i
        # rename map: the agent referenced a title an earlier op renamed.
        if ctx is not None:
            mapped = ctx.get("rename_map", {}).get(title)
            if mapped:
                for i, s in enumerate(slides):
                    if str(s.get("title", "")) == mapped:
                        return i
        close = difflib.get_close_matches(
            title, _outline_titles(slides), n=1, cutoff=_FUZZY_CUTOFF,
        )
        if close:
            return next(i for i, s in enumerate(slides)
                        if str(s.get("title", "")) == close[0])
        raise _OpError(
            f"no outline slide titled {title!r}. "
            f"Current titles: {_outline_titles(slides)}"
        )
    raise _OpError("op needs a 'title' or 'index'")


def _norm_outline_slide(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise _OpError("slide must be an object")
    slide = {
        "title": str(raw.get("title", "")).strip(),
        "content": str(raw.get("content", "")),
        "discussion_idea": str(raw.get("discussion_idea", "")).strip(),
    }
    if not slide["title"]:
        raise _OpError("outline slide needs a non-empty 'title'")
    if not slide["discussion_idea"]:
        raise _OpError("outline slide needs a non-empty 'discussion_idea'")
    return slide


def _ol_record_rename(ctx, old: str, new: str) -> None:
    """Remember that ``old`` was renamed to ``new`` so later ops in the same
    batch can still resolve a reference to the old title."""
    if ctx is not None and old and old != new:
        ctx.setdefault("rename_map", {})[old] = new


def _ol_edit_slide(slides, op, ctx, warnings):
    idx = _outline_resolve(op, slides, ctx)
    s = slides[idx]
    orig_title = str(s.get("title", ""))
    changed: List[str] = []

    new_title = op.get("new_title")
    if new_title is not None:
        nt = str(new_title).strip()
        if not nt:
            raise _OpError("'new_title' cannot be empty")
        if nt != orig_title:
            s["title"] = nt
            _ol_record_rename(ctx, orig_title, nt)
            changed.append("title")

    for key in ("content", "discussion_idea"):
        if key in op and op[key] is not None:
            val = str(op[key])
            if key == "discussion_idea" and not val.strip():
                raise _OpError("'discussion_idea' cannot be emptied")
            if val != s.get(key, ""):
                s[key] = val
                changed.append(key)

    if not changed:
        # 'title' is the identity selector, NOT a rename field. If the
        # agent passed a 'title' that differs from the slide's current title
        # and gave no 'new_title', it almost certainly meant to rename —
        # surface that clearly instead of silently reporting ok.
        raw_title = op.get("title")
        if raw_title is not None and new_title is None and str(raw_title) != orig_title:
            raise _OpError(
                "edit_slide's 'title' field only SELECTS the slide, it does "
                "not rename it — to rename, use the 'retitle' op or pass "
                "'new_title'"
            )
        raise _OpError(
            "edit_slide changed nothing — to rename pass 'new_title' (or use "
            "the 'retitle' op); to edit the body pass 'content' / "
            "'discussion_idea'"
        )


def _ol_retitle(slides, op, ctx, warnings):
    idx = _outline_resolve(op, slides, ctx)
    s = slides[idx]
    new_title = str(op.get("new_title", "")).strip()
    if not new_title:
        raise _OpError("'new_title' is required")
    old = str(s.get("title", ""))
    if new_title == old:
        warnings.append("new_title equals the current title — no change")
        return
    s["title"] = new_title
    _ol_record_rename(ctx, old, new_title)


def _ol_add_slide(slides, op, ctx, warnings):
    slide = _norm_outline_slide(op.get("slide"))
    at = op.get("at_index")
    pos = len(slides) if at is None else max(0, min(int(at), len(slides)))
    slides.insert(pos, slide)


def _ol_remove_slide(slides, op, ctx, warnings):
    idx = _outline_resolve(op, slides, ctx)
    slides.pop(idx)


def _ol_move_slide(slides, op, ctx, warnings):
    """Relocate one outline slide. Use this instead of a full ``reorder`` for
    the common 'move slide X after slide Y' request — no need to enumerate
    every slide, so a single typo cannot sink the whole edit."""
    idx = _outline_resolve(op, slides, ctx)
    if "after" in op and op["after"] is not None:
        anchor = _outline_resolve(_as_ref(op["after"]), slides, ctx)
        if anchor == idx:
            raise _OpError("cannot move a slide relative to itself")
        slide = slides.pop(idx)
        if anchor > idx:
            anchor -= 1
        slides.insert(anchor + 1, slide)
    elif "before" in op and op["before"] is not None:
        anchor = _outline_resolve(_as_ref(op["before"]), slides, ctx)
        if anchor == idx:
            raise _OpError("cannot move a slide relative to itself")
        slide = slides.pop(idx)
        if anchor > idx:
            anchor -= 1
        slides.insert(anchor, slide)
    elif op.get("to_index") is not None:
        slide = slides.pop(idx)
        pos = max(0, min(int(op["to_index"]), len(slides)))
        slides.insert(pos, slide)
    else:
        raise _OpError("move_slide needs 'after', 'before', or 'to_index'")


def _ol_reorder(slides, op, ctx, warnings):
    order = op.get("order")
    if not isinstance(order, list):
        raise _OpError("'order' must be a list")
    if len(order) != len(slides):
        raise _OpError(
            f"'order' must list every slide exactly once ({len(slides)} "
            f"slides, got {len(order)}). For a single relocation use the "
            f"'move_slide' op instead. Current titles: {_outline_titles(slides)}"
        )
    picked, seen = [], set()
    for ref in order:
        idx = ref if isinstance(ref, int) else _outline_resolve(_as_ref(ref), slides, ctx)
        if not 0 <= idx < len(slides):
            raise _OpError(f"index {idx} out of range")
        if idx in seen:
            raise _OpError(f"slide {ref!r} listed twice")
        seen.add(idx)
        picked.append(slides[idx])
    slides[:] = picked


def _ol_split_slide(slides, op, ctx, warnings):
    idx = _outline_resolve(op, slides, ctx)
    parts = op.get("parts")
    if not isinstance(parts, list) or len(parts) < 2:
        raise _OpError("'parts' must list >=2 slides")
    new = [_norm_outline_slide(p) for p in parts]
    slides[idx:idx + 1] = new


def _ol_merge_slides(slides, op, ctx, warnings):
    refs = op.get("targets")
    if not isinstance(refs, list) or len(refs) < 2:
        raise _OpError("'targets' must list >=2 slides")
    idxs = sorted({_outline_resolve(_as_ref(r), slides, ctx) for r in refs})
    if len(idxs) < 2:
        raise _OpError("merge targets resolved to fewer than 2 distinct slides")
    raw_merged = dict(op.get("merged") or {})
    # Token-saving path: if the agent omits 'content', concatenate the source
    # slides' content in Python — no need for the LLM to re-type paragraphs.
    if not str(raw_merged.get("content", "")).strip():
        raw_merged["content"] = "\n\n".join(
            slides[i]["content"] for i in idxs if slides[i].get("content")
        )
        warnings.append("merged 'content' auto-concatenated from source slides")
    merged = _norm_outline_slide(raw_merged)
    for i in reversed(idxs):
        slides.pop(i)
    slides.insert(idxs[0], merged)


_OUTLINE_OPS = {
    "edit_slide": _ol_edit_slide,
    "retitle": _ol_retitle,
    "add_slide": _ol_add_slide,
    "remove_slide": _ol_remove_slide,
    "move_slide": _ol_move_slide,
    "reorder": _ol_reorder,
    "split_slide": _ol_split_slide,
    "merge_slides": _ol_merge_slides,
}


def apply_outline_ops(
    slides: List[Dict[str, Any]],
    ops: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[OpResult]]:
    """Apply a batch of edit ops to a raw_content_rst outline.

    ``slides`` (list of ``{title, content, discussion_idea}``) is not mutated.
    Returns ``(new_slides, results)``.
    """
    if not isinstance(slides, list):
        raise ValueError("outline must be a list of slide dicts")
    work = copy.deepcopy(slides)
    # rename_map lets a later op in the same batch still resolve a title that
    # an earlier op renamed away from.
    ctx: Dict[str, Any] = {"rename_map": {}}
    results: List[OpResult] = []
    for i, op in enumerate(ops or []):
        op_type = (op or {}).get("op", "")
        target = str(op.get("title") or op.get("index", "")
                     or (op.get("slide") or {}).get("title", ""))
        res = OpResult(index=i, op=op_type, target=target)
        handler = _OUTLINE_OPS.get(op_type)
        if handler is None:
            res.ok = False
            res.error = f"unknown op {op_type!r}; valid: {sorted(_OUTLINE_OPS)}"
            results.append(res)
            continue
        before = copy.deepcopy(work)
        try:
            handler(work, op, ctx, res.warnings)
        except _OpError as exc:
            res.ok = False
            res.error = str(exc)
            work[:] = before
        except Exception as exc:             # pragma: no cover - defensive
            res.ok = False
            res.error = f"unexpected error: {exc}"
            work[:] = before
        results.append(res)
    return work, results
