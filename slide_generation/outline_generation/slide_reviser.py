#!/usr/bin/env python3
"""
Section-wise Slide Reviser
==========================

This module post-processes the output of `SlidePlanner` to improve the
*global* flow and structure of a paper presentation. It is intended to be used
after planning paragraphs into slides per section.

Typical use:

    from slide_planner import SlidePlanner, SlidePlannerConfig
    from slide_reviser import SlideReviser

    planner = SlidePlanner(cfg)
    results = planner.plan_from_rst_output(rst_output_dir)

    reviser = SlideReviser(cfg)
    results_revised = reviser.revise_plan(results, rst_output_dir)

What it does
------------
- Reads paragraph text from `rst_output_dir/<section_key>/paragraphs.json`
- Uses the initial planned slides (`results["merged_slides"]`) as a proposal
- Asks an LLM to revise the merged slide sequence to improve narrative flow
  (Intro -> Related Work -> Method -> Experiments -> Results -> Conclusion, etc.)
- Validates the output (paragraph coverage, duplicates, unknown paragraph ids)
- Writes `revised_merged_slides.json` and `revised_summary.json` into output_dir

Notes
-----
- The reviser *does not* change the underlying RST parse; it only rearranges/
  merges/splits slides at the paragraph-name level.
- Output is JSON only (no markdown) from the LLM.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from .slide_planner import SlidePlannerConfig
from .providers import get_provider, extract_json_from_response, load_prompt


@dataclass
class SlideReviserConfig:
    """
    Optional extra knobs for slide revision. Most settings are inherited from
    SlidePlannerConfig (provider/model/output_dir/etc.).
    """
    paragraph_snippet_chars: int = 280
    min_paragraphs_per_slide: int = 1
    max_paragraphs_per_slide: int = 5
    enforce_full_coverage: bool = True
    save_intermediate: bool = True


# =============================================================================
# Prompt Template (loaded from external file)
# =============================================================================

REVISER_PROMPT = load_prompt("slide_reviser.txt")


def _json_dumps_or_null(obj: Optional[Dict[str, Any]]) -> str:
    """Safely dump a dict to JSON for prompt inclusion."""
    if obj is None:
        return "null"
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return json.dumps(str(obj), ensure_ascii=False)


def _build_summary_from_merged_slides(merged_slides: List[Dict[str, Any]], presentation_length: int) -> Dict[str, Any]:
    """Build a minimal summary matching downstream expectations."""
    total_slides = len(merged_slides)
    section_set = {s.get("section") for s in merged_slides if s.get("section")}
    return {
        "total_slides": total_slides,
        "total_sections": len(section_set),
        "presentation_length": presentation_length,
    }

# =============================================================================
# Helpers
# =============================================================================

def _safe_read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _format_section_inventory(sections: List[Tuple[str, str]]) -> str:
    lines = []
    for i, (k, t) in enumerate(sections, start=1):
        lines.append(f"  {i}. {k} : {t}")
    return "\n".join(lines)


def _format_current_slides(slides: List[Dict[str, Any]]) -> str:
    lines = []
    for s in slides:
        sn = s.get("slide_number", "?")
        sec = s.get("section", "")
        ttl = s.get("title", "Untitled")
        paras = s.get("paragraphs", [])
        idea = str(s.get("discussion_idea") or "").strip()
        if idea:
            lines.append(f"- Slide {sn} [{sec}] {ttl} :: {paras}")
            lines.append(f"    discussion_idea: {idea}")
        else:
            lines.append(f"- Slide {sn} [{sec}] {ttl} :: {paras}")
    return "\n".join(lines)


def _make_paragraph_snippets(
    paragraph_map: Dict[str, str],
    *,
    max_chars: int,
) -> str:
    lines = []
    for pid in sorted(paragraph_map.keys()):
        txt = paragraph_map[pid].replace("\n", " ").strip()
        lines.append(f"[{pid}] {txt}")
    return "\n".join(lines)


def _collect_paragraphs_from_rst_output(rst_output_dir: Path) -> Dict[str, str]:
    """
    Collect paragraph text from section subdirectories:
        rst_output_dir/<section_key>/paragraphs.json
    Returns a merged dict: {paragraph_id: paragraph_text}
    """
    results_path = rst_output_dir / "results.json"
    if not results_path.exists():
        raise FileNotFoundError(f"results.json not found in {rst_output_dir}")

    rst_results = _safe_read_json(results_path)
    paragraph_map: Dict[str, str] = {}

    for section_key in rst_results.keys():
        section_dir = rst_output_dir / section_key
        paras_path = section_dir / "paragraphs.json"
        if not paras_path.exists():
            continue
        paras = _safe_read_json(paras_path)
        if isinstance(paras, dict):
            paragraph_map.update({str(k): str(v) for k, v in paras.items()})

    return paragraph_map


def _collect_section_titles(rst_output_dir: Path) -> List[Tuple[str, str]]:
    """
    Extract section ordering and titles from rst_output_dir/results.json
    Returns list[(section_key, title)] in file order.
    """
    results_path = rst_output_dir / "results.json"
    if not results_path.exists():
        raise FileNotFoundError(f"results.json not found in {rst_output_dir}")

    rst_results = _safe_read_json(results_path)
    sections: List[Tuple[str, str]] = []
    for section_key, section_result in rst_results.items():
        title = section_result.get("title", section_key)
        sections.append((section_key, title))
    return sections


def _validate_slides(
    slides: List[Dict[str, Any]],
    *,
    all_paragraph_ids: List[str],
) -> Tuple[bool, List[str]]:
    """
    Validate that slides cover all paragraphs exactly once and only use known IDs.
    Returns (ok, errors).
    """
    errors: List[str] = []
    known = set(all_paragraph_ids)

    seen: List[str] = []
    for i, slide in enumerate(slides, start=1):
        paras = slide.get("paragraphs", [])
        if not isinstance(paras, list):
            errors.append(f"Slide {i} paragraphs is not a list")
            continue
        for pid in paras:
            if pid not in known:
                errors.append(f"Unknown paragraph id: {pid}")
            seen.append(pid)

    from collections import Counter
    c = Counter(seen)
    dups = [p for p, n in c.items() if n > 1]
    if dups:
        errors.append(f"Duplicate paragraph ids: {dups[:20]}{' ...' if len(dups) > 20 else ''}")

    missing = [p for p in all_paragraph_ids if p not in c]
    if missing:
        errors.append(f"Missing paragraph ids: {missing[:20]}{' ...' if len(missing) > 20 else ''}")

    return (len(errors) == 0), errors


def _repair_slides_greedy(
    slides: List[Dict[str, Any]],
    *,
    all_paragraph_ids: List[str],
) -> List[Dict[str, Any]]:
    """
    Minimal repair strategy:
    - Remove duplicates (keep first occurrence)
    - Append missing paragraphs to the last slide (or create a new slide)
    This is a safety net; best effort only.
    """
    known = set(all_paragraph_ids)
    seen = set()
    repaired: List[Dict[str, Any]] = []

    for slide in slides:
        paras = slide.get("paragraphs", [])
        if not isinstance(paras, list):
            paras = []
        new_paras = []
        for pid in paras:
            if pid in known and pid not in seen:
                seen.add(pid)
                new_paras.append(pid)
        new_slide = dict(slide)
        new_slide["paragraphs"] = new_paras
        repaired.append(new_slide)

    missing = [p for p in all_paragraph_ids if p not in seen]
    if missing:
        if repaired:
            repaired[-1]["paragraphs"] = repaired[-1].get("paragraphs", []) + missing
            repaired[-1]["rationale"] = (repaired[-1].get("rationale", "") + " (auto-repair: appended missing paragraphs)").strip()
        else:
            repaired = [{
                "slide_number": 1,
                "section": "unknown",
                "section_title": "Unknown",
                "title": "Recovered Content",
                "paragraphs": missing,
                "rationale": "Auto-repair: created a slide for missing paragraphs.",
            }]

    for i, s in enumerate(repaired, start=1):
        s["slide_number"] = i

    return repaired


# =============================================================================
# Main Class
# =============================================================================

class SlideReviser:
    """
    Post-processes merged slides from SlidePlanner to improve global flow.
    """

    REVISER_SYSTEM_MSG = "You are an expert at organizing academic content into presentation slides. Output only valid JSON."

    def __init__(
        self,
        config: Optional[SlidePlannerConfig] = None,
        reviser_config: Optional[SlideReviserConfig] = None,
    ):
        self.config = config or SlidePlannerConfig()
        self.reviser_config = reviser_config or SlideReviserConfig()
        self.output_dir = Path(self.config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._provider = None

    @property
    def provider(self):
        if self._provider is None:
            self._provider = get_provider(self.config, self.REVISER_SYSTEM_MSG)
        return self._provider

    def revise_plan(self, results: Dict[str, Any], rst_output_dir: str | Path, *, commentary: Optional[Dict[str, Any]] = None, judge_feedback: Optional[Dict[str, Any]] = None, round_number: int = 0) -> Dict[str, Any]:
        """
        Revise/improve the merged slide plan.

        Args:
            results: output of SlidePlanner.plan_from_rst_output(...)
                     expected keys: merged_slides, section_results, summary
            rst_output_dir: path to discourse parser output dir

        Returns:
            dict with revised_merged_slides, revised_summary, and output paths.
        """
        rst_output_dir = Path(rst_output_dir)

        if "merged_slides" not in results:
            raise ValueError("Expected `results` to contain `merged_slides` from SlidePlanner")

        current_slides: List[Dict[str, Any]] = results.get("merged_slides", [])
        paragraph_map = _collect_paragraphs_from_rst_output(rst_output_dir)
        all_paragraph_ids = sorted(paragraph_map.keys())

        sections = _collect_section_titles(rst_output_dir)
        section_inventory = _format_section_inventory(sections)

        prompt = REVISER_PROMPT.format(
            audience=self.config.audience,
            presentation_length=self.config.presentation_length,
            section_inventory=section_inventory,
            current_slides=_format_current_slides(current_slides),
            paragraph_snippets=_make_paragraph_snippets(
                paragraph_map, max_chars=self.reviser_config.paragraph_snippet_chars
            ),
            commentary_json=_json_dumps_or_null(commentary),
            judge_feedback_json=_json_dumps_or_null(judge_feedback),
            min_paras=self.reviser_config.min_paragraphs_per_slide,
            max_paras=self.reviser_config.max_paragraphs_per_slide,
        )

        if self.reviser_config.save_intermediate and self.config.save_intermediate:
            (self.output_dir / "slide_reviser_prompt.txt").write_text(prompt, encoding="utf-8")

        raw = self.provider.generate(prompt)

        if raw is None:
            out = dict(results)
            out["merged_slides"] = current_slides
            out["summary"] = _build_summary_from_merged_slides(current_slides, self.config.presentation_length)
            out["_reviser"] = {
                "success": False,
                "error": "LLM provider returned None.",
                "used_critic": commentary is not None,
                "used_judge": judge_feedback is not None,
            }
            return out

        if isinstance(raw, (dict, list)):
            raw_text = json.dumps(raw, ensure_ascii=False, indent=2)
        else:
            raw_text = str(raw)

        if self.reviser_config.save_intermediate and self.config.save_intermediate:
            (self.output_dir / "slide_reviser_raw_response.txt").write_text(raw_text, encoding="utf-8")

        try:
            parsed = extract_json_from_response(raw_text)
        except Exception as e:
            out = dict(results)
            out["merged_slides"] = current_slides
            out["summary"] = _build_summary_from_merged_slides(current_slides, self.config.presentation_length)
            out["_reviser"] = {"success": False, "error": f"Failed to parse JSON from LLM response: {e}", "used_critic": commentary is not None, "used_judge": judge_feedback is not None}
            return out

        slides = parsed.get("slides", [])
        if not isinstance(slides, list) or not slides:
            out = dict(results)
            out["merged_slides"] = current_slides
            out["summary"] = _build_summary_from_merged_slides(current_slides, self.config.presentation_length)
            out["_reviser"] = {"success": False, "error": "LLM returned empty or invalid `slides` list", "used_critic": commentary is not None, "used_judge": judge_feedback is not None}
            return out

        ok, errors = _validate_slides(slides, all_paragraph_ids=all_paragraph_ids)
        if not ok and self.reviser_config.enforce_full_coverage:
            slides = _repair_slides_greedy(slides, all_paragraph_ids=all_paragraph_ids)
            ok2, errors2 = _validate_slides(slides, all_paragraph_ids=all_paragraph_ids)
            errors = errors + ["Auto-repair applied."] + ([] if ok2 else errors2)
            ok = ok2

        section_title_map = {k: t for k, t in sections}
        for s in slides:
            sec = s.get("section")
            if sec and not s.get("section_title"):
                s["section_title"] = section_title_map.get(sec, sec)

        for s in slides:
            di = str(s.get("discussion_idea") or "").strip()
            if di:
                s["discussion_idea"] = di
                continue
            paras = s.get("paragraphs") or []
            snippets: List[str] = []
            for pid in paras[:3]:
                if not isinstance(pid, str):
                    continue
                raw = paragraph_map.get(pid, "").replace("\n", " ").strip()
                if raw:
                    snippets.append(raw[:200] + ("…" if len(raw) > 200 else ""))
            if snippets:
                s["discussion_idea"] = (
                    "Oral beat (auto): " + " · ".join(snippets)[:600]
                )
            else:
                s["discussion_idea"] = (
                    f"Oral beat (auto): Develop the slide titled \"{s.get('title', 'Untitled')}\" "
                    f"from the grouped source paragraphs."
                )

        for i, s in enumerate(slides, start=1):
            s["slide_number"] = i

        revised_summary = {
            "total_slides": len(slides),
            "total_sections": len({s.get("section") for s in slides if s.get("section")}),
            "total_paragraphs": len(all_paragraph_ids),
            "presentation_length": self.config.presentation_length,
            "validation_ok": ok,
            "validation_errors": errors,
        }

        revised_output = {
            "presentation_length": self.config.presentation_length,
            "audience": self.config.audience,
            "total_slides": len(slides),
            "slides": slides,
            "validation": {
                "ok": ok,
                "errors": errors,
            },
        }

        revised_merged_path = self.output_dir / f"revised_merged_slides_round_{round_number}.json"
        revised_summary_path = self.output_dir / f"revised_summary_round_{round_number}.json"
        revised_merged_path.write_text(json.dumps(revised_output, ensure_ascii=False, indent=2), encoding="utf-8")
        revised_summary_path.write_text(json.dumps(revised_summary, ensure_ascii=False, indent=2), encoding="utf-8")

        out = dict(results)
        out["merged_slides"] = slides
        out["summary"] = {
            "total_slides": revised_summary["total_slides"],
            "total_sections": revised_summary["total_sections"],
            "total_paragraphs": revised_summary["total_paragraphs"],
            "presentation_length": self.config.presentation_length,
        }
        out["_reviser"] = {
            "success": True,
            "validation_ok": ok,
            "validation_errors": errors,
            "used_critic": commentary is not None,
            "used_judge": judge_feedback is not None,
            "revised_merged_path": str(revised_merged_path),
            "revised_summary_path": str(revised_summary_path),
        }
        # Backward-compatible fields
        out["success"] = True
        out["revised_merged_slides"] = slides
        out["revised_output"] = revised_output
        out["revised_summary"] = revised_summary
        out["revised_merged_path"] = str(revised_merged_path)
        out["revised_summary_path"] = str(revised_summary_path)
        return out
