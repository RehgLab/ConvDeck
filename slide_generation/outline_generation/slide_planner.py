#!/usr/bin/env python3
"""
Section-wise Slide Planner
==========================

This module groups paragraphs into slides on a per-section basis, using the
RST relations from section_discourse_parser as guidance. Each section produces
its own slide list, which can later be merged into a full presentation.

Usage:
    python slide_planner.py section_discourse_output/ --output-dir slides_output/
    python slide_planner.py section_discourse_output/ --presentation-length 20
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class SlidePlannerConfig:
    """Configuration for section-wise slide planner."""
    # LLM provider settings
    provider: Literal["openai", "anthropic", "vllm"] = "openai"
    model: str = "gpt-4o"
    api_key: Optional[str] = None
    api_base_url: Optional[str] = None
    temperature: float = 0.1
    max_tokens: int = 4096
    
    # Output settings
    output_dir: str = "outputs/section_slides_output"
    save_intermediate: bool = True
    
    # Presentation metadata
    presentation_length: int = 20  # Total presentation length in minutes
    audience: str = "researchers"
    
    # Slide hints per section (optional)
    min_slides_per_section: Optional[int] = None
    max_slides_per_section: Optional[int] = None


from .providers import LLMProvider, get_provider, extract_json_from_response, load_prompt


# =============================================================================
# Prompt Template
# =============================================================================

SECTION_SLIDE_PROMPT = load_prompt("slide_planner.txt")


# =============================================================================
# Helper Functions
# =============================================================================

def format_relations(relations: List[Dict[str, Any]]) -> str:
    """
    Format relations for the slide-grouper prompt (edge-style).

    Expects relations produced by extract_relations_from_json_tree(tree), which contains:
      - type == "child_to_group": {from, to, role, group_relation}
      - (optionally) type == "group_definition" (ignored here)
    """
    if not relations:
        return "No explicit relations (treat paragraphs as sequential)"

    lines: List[str] = []
    for r in relations:
        if r.get("type") != "child_to_group":
            continue
        frm = r.get("from")
        to = r.get("to")
        role = r.get("role")
        grel = r.get("group_relation")
        lines.append(f"  {frm} --[{role} @ {grel}]--> {to}")

    return "\n".join(lines) if lines else "No explicit relations (treat paragraphs as sequential)"


def format_paragraphs(paragraphs: Dict[str, str]) -> str:
    """Format paragraphs for the prompt."""
    lines = []
    for name, text in paragraphs.items():
        truncated = text
        truncated = truncated.replace("\n", " ")
        lines.append(f"[{name}]\n{truncated}\n")
    return "\n".join(lines)


def estimate_section_time(
    section_paragraphs: int,
    total_paragraphs: int,
    total_time: int
) -> str:
    """Estimate time allocation for a section based on content."""
    if total_paragraphs == 0:
        return "1-2 minutes"
    
    proportion = section_paragraphs / total_paragraphs
    estimated_time = max(1, round(proportion * total_time))
    
    if estimated_time <= 2:
        return "1-2 minutes (brief)"
    elif estimated_time <= 4:
        return f"~{estimated_time} minutes"
    else:
        return f"{estimated_time-1}-{estimated_time+1} minutes"


# =============================================================================
# Main Class
# =============================================================================

class SlidePlanner:
    """
    Groups paragraphs into slides on a per-section basis using RST relations.
    """
    
    def __init__(self, config: Optional[SlidePlannerConfig] = None):
        self.config = config or SlidePlannerConfig()
        self.output_dir = Path(self.config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._provider = None
    
    PLANNER_SYSTEM_MSG = "You are an expert at organizing academic content into presentation slides. Output only valid JSON."

    @property
    def provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = get_provider(self.config, self.PLANNER_SYSTEM_MSG)
        return self._provider

    def plan_section(
        self,
        section_key: str,
        section_data: Dict[str, Any],
        section_number: int,
        total_sections: int,
        total_paragraphs: int,
    ) -> Dict[str, Any]:
        """
        Group paragraphs into slides for a single section.

        section_data expects:
        - title: str
        - subsections: list[str] | str
        - paragraphs: dict[str, str]     (paragraph_id -> text)
        - section_tree: dict | None      (raw JSON from section_tree.json)
        """
        title = section_data.get("title", section_key)
        subsections = section_data.get("subsections", [title])
        paragraphs: Dict[str, str] = section_data.get("paragraphs", {}) or {}
        section_tree: Dict[str, Any] | None = section_data.get("section_tree")

        if not paragraphs:
            return {"success": False, "error": "No paragraphs in section", "slides": []}

        num_paragraphs = len(paragraphs)

        # Estimate time and slides for this section
        section_time = estimate_section_time(
            num_paragraphs, total_paragraphs, self.config.presentation_length
        )

        # Slide hint
        if self.config.min_slides_per_section and self.config.max_slides_per_section:
            slide_hint = f"{self.config.min_slides_per_section}-{self.config.max_slides_per_section} slides"
        else:
            estimated_slides = max(1, num_paragraphs // 3)  # ~3 paragraphs per slide
            slide_hint = f"approximately {estimated_slides} slide(s)"

        # Pass tree JSON directly into the prompt (or a placeholder if missing)
        if section_tree is None:
            section_tree_json = "null"
        else:
            section_tree_json = json.dumps(section_tree, ensure_ascii=False, indent=2)

        # Build prompt (NOTE: SECTION_SLIDE_PROMPT must have {section_tree_json})
        prompt = SECTION_SLIDE_PROMPT.format(
            section_title=title,
            subsections=", ".join(subsections) if isinstance(subsections, list) else subsections,
            num_paragraphs=num_paragraphs,
            section_number=section_number,
            total_sections=total_sections,
            presentation_length=self.config.presentation_length,
            audience=self.config.audience,
            section_time_hint=section_time,
            section_tree_json=section_tree_json,         # <-- direct tree injection
            paragraph_content=format_paragraphs(paragraphs),
            slide_hint=slide_hint,
        )

        # Save intermediate
        section_output_dir = self.output_dir / section_key
        section_output_dir.mkdir(parents=True, exist_ok=True)
        if self.config.save_intermediate:
            (section_output_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

        # Call LLM
        try:
            response = self.provider.generate(prompt)
            if self.config.save_intermediate:
                (section_output_dir / "llm_response.txt").write_text(response, encoding="utf-8")

            slides_data = extract_json_from_response(response)
            slides = slides_data.get("section_slides", [])

        except Exception as e:
            return {"success": False, "error": str(e), "slides": []}

        # Save slides (no extra checks)
        output_path = section_output_dir / "slides.json"
        output_data = {
            "section_key": section_key,
            "section_title": title,
            "subsections": subsections,
            "slides": slides,
            "stats": {
                "num_slides": len(slides),
                "num_paragraphs": num_paragraphs,
                "has_section_tree": section_tree is not None,
            },
        }
        output_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "success": True,
            "section_key": section_key,
            "section_title": title,
            "slides": slides,
            "stats": output_data["stats"],
        }


    def plan_from_rst_output(self, rst_output_dir: str | Path) -> Dict[str, Any]:
        """
        Plan slides for all sections from discourse parser output.

        Expected per-section files (in rst_output_dir/<section_key>/):
        - paragraphs.json   (dict paragraph_id -> paragraph_text)
        - section_tree.json (raw JSON tree: {"root":"gK","groups":{...}}) [optional]
        """
        rst_output_dir = Path(rst_output_dir)
        results_path = rst_output_dir / "results.json"

        if not results_path.exists():
            raise FileNotFoundError(f"results.json not found in {rst_output_dir}")

        rst_results = json.loads(results_path.read_text(encoding="utf-8"))

        # Count total paragraphs and build section inputs
        total_paragraphs = 0
        sections_to_process: List[Dict[str, Any]] = []

        for section_key, section_result in rst_results.items():
            section_dir = rst_output_dir / section_key
            paras_path = section_dir / "paragraphs.json"
            tree_path = section_dir / "section_tree.json"

            paragraphs: Dict[str, str] = {}
            section_tree: Dict[str, Any] | None = None

            if paras_path.exists():
                paragraphs = json.loads(paras_path.read_text(encoding="utf-8"))
            total_paragraphs += len(paragraphs)

            if tree_path.exists():
                try:
                    section_tree = json.loads(tree_path.read_text(encoding="utf-8"))
                except Exception:
                    section_tree = None

            sections_to_process.append(
                {
                    "key": section_key,
                    "title": section_result.get("title", section_key),
                    "paragraphs": paragraphs,
                    "section_tree": section_tree,  # <-- pass through
                    "subsections": section_result.get("subsections", []),
                }
            )

        print(f"Found {len(sections_to_process)} sections with {total_paragraphs} total paragraphs")

        # Process each section
        all_results: Dict[str, Any] = {}

        for idx, section in enumerate(sections_to_process, 1):
            print(f"\nPlanning section: {section['key']} ({section['title']})")
            print(f"  Paragraphs: {len(section['paragraphs'])}")
            print(f"  Has section_tree: {bool(section.get('section_tree'))}")

            result = self.plan_section(
                section_key=section["key"],
                section_data={
                    "title": section["title"],
                    "paragraphs": section["paragraphs"],
                    "section_tree": section.get("section_tree"),
                    "subsections": section["subsections"],
                },
                section_number=idx,
                total_sections=len(sections_to_process),
                total_paragraphs=total_paragraphs,
            )

            all_results[section["key"]] = result

            if result.get("success"):
                print(f"  Generated: {len(result.get('slides', []))} slides")
            else:
                print(f"  Failed: {result.get('error', 'Unknown')}")

        # Create merged output
        merged_slides: List[Dict[str, Any]] = []
        slide_number = 1

        for section_key in [s["key"] for s in sections_to_process]:
            result = all_results.get(section_key, {})
            if result.get("success"):
                for slide in result.get("slides", []):
                    merged_slides.append(
                        {
                            "slide_number": slide_number,
                            "section": section_key,
                            "section_title": result.get("section_title", section_key),
                            "title": slide.get("title", "Untitled"),
                            "paragraphs": slide.get("paragraphs", []),
                            "rationale": slide.get("rationale", ""),
                        }
                    )
                    slide_number += 1

        merged_output = {
            "presentation_length": self.config.presentation_length,
            "audience": self.config.audience,
            "total_slides": len(merged_slides),
            "sections_processed": len(sections_to_process),
            "slides": merged_slides,
            "section_results": {
                k: {"success": v.get("success"), "stats": v.get("stats")}
                for k, v in all_results.items()
            },
        }

        merged_path = self.output_dir / "merged_slides.json"
        merged_path.write_text(json.dumps(merged_output, ensure_ascii=False, indent=2), encoding="utf-8")

        summary = {
            "total_slides": len(merged_slides),
            "total_sections": len(sections_to_process),
            "total_paragraphs": total_paragraphs,
            "presentation_length": self.config.presentation_length,
            "sections": [
                {
                    "key": s["key"],
                    "title": s["title"],
                    "paragraphs": len(s["paragraphs"]),
                    "slides": len(all_results.get(s["key"], {}).get("slides", [])),
                    "has_section_tree": bool(s.get("section_tree")),
                }
                for s in sections_to_process
            ],
        }

        summary_path = self.output_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "merged_slides": merged_slides,
            "section_results": all_results,
            "summary": summary,
            "merged_path": str(merged_path),
            "summary_path": str(summary_path),
        }
