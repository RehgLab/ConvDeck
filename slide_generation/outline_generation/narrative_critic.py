#!/usr/bin/env python3
"""\
Section-wise Narrative Critic
=============================

This module critiques the *global* structure and coherence of a slide plan
produced by `SlidePlanner`.

It reads paragraph text from `rst_output_dir/<section_key>/paragraphs.json`,
then asks an LLM to produce actionable critique about:
- overall deck flow (Intro → Related Work → Method → Experiments → Results → Conclusion)
- missing/overweight sections for an academic paper presentation
- per-slide coherence (too broad, too narrow, wrong grouping)
- redundancy / duplicated content
- title quality and transitions

It does NOT modify the slide plan; see `SlideReviser` for that.

Typical use:

    from slide_planner import SlidePlanner, SlidePlannerConfig
    from narrative_critic import NarrativeCritic

    planner = SlidePlanner(cfg)
    results = planner.plan_from_rst_output(rst_output_dir)

    critic = NarrativeCritic(cfg)
    critique = critic.critique_plan(results, rst_output_dir)

"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


from .slide_planner import SlidePlannerConfig
from .providers import get_provider, extract_json_from_response, load_prompt


# =============================================================================
# Narrative Critic config
# =============================================================================

@dataclass
class NarrativeCriticConfig:
    """Narrative-critic-specific knobs. Uses the same base config for provider."""

    enabled: bool = True
    max_chars_per_paragraph: int = 700
    max_slides_with_full_text: int = 18
    save_intermediate: bool = True


# =============================================================================
# Prompts (loaded from external files)
# =============================================================================

CRITIC_PROMPT = load_prompt("narrative_critic.txt")
CRITIC_PROMPT_NO_COMMITMENT = load_prompt("narrative_critic_no_commitment.txt")

# =============================================================================
# Implementation
# =============================================================================

class NarrativeCritic:
    CRITIC_SYSTEM_MSG = "You are a meticulous academic presentation reviewer. Output only valid JSON."

    def __init__(
        self,
        config: SlidePlannerConfig,
        critic_config: Optional[NarrativeCriticConfig] = None,
    ):
        self.config = config
        self.critic_config = critic_config or NarrativeCriticConfig()
        self.provider = get_provider(config, self.CRITIC_SYSTEM_MSG)

        self.output_dir = Path(self.config.output_dir) / "narrative_critic"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _load_paragraph_texts(self, rst_output_dir: str) -> Dict[str, str]:
        """Load paragraph texts from rst_output_dir/<section_key>/paragraphs.json."""
        base = Path(rst_output_dir)
        if not base.exists():
            raise FileNotFoundError(f"rst_output_dir not found: {rst_output_dir}")

        para_map: Dict[str, str] = {}
        for section_dir in sorted([p for p in base.iterdir() if p.is_dir()]):
            pj = section_dir / "paragraphs.json"
            if not pj.exists():
                continue
            data = json.loads(pj.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, str):
                        para_map[k] = v
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item and "text" in item:
                        para_map[str(item["name"])] = str(item["text"])
        return para_map

    def _collect_used_paragraphs(self, merged_slides: List[Dict[str, Any]]) -> List[str]:
        used: List[str] = []
        for s in merged_slides:
            for p in s.get("paragraphs", []) or []:
                if isinstance(p, str):
                    used.append(p)
        seen = set()
        out = []
        for p in used:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _make_snippets(self, para_map: Dict[str, str], paragraph_ids: List[str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for pid in paragraph_ids:
            txt = para_map.get(pid, "")
            if not txt:
                continue
            out[pid] = txt
        return out

    def critique_plan(self, results: Dict[str, Any], rst_output_dir: str, commitment_md: Optional[str] = None, use_commitment_building: bool = False, round_number: int = 0) -> Dict[str, Any]:
        """Return critique JSON + file paths."""
        if not self.critic_config.enabled:
            return {
                "success": False,
                "error": "critic disabled",
                "commentary": None,
                "commentary_path": None,
            }

        merged_slides = results.get("merged_slides") or results.get("slides")
        if not isinstance(merged_slides, list) or len(merged_slides) == 0:
            return {
                "success": False,
                "error": "results has no merged_slides",
                "commentary": None,
                "commentary_path": None,
            }

        para_map = self._load_paragraph_texts(rst_output_dir)
        used_paras = self._collect_used_paragraphs(merged_slides)
        snippets = self._make_snippets(para_map, used_paras)

        section_order = []
        seen_sec = set()
        for s in merged_slides:
            sec = s.get("section")
            if isinstance(sec, str) and sec not in seen_sec:
                seen_sec.add(sec)
                section_order.append(sec)

        if use_commitment_building:
            prompt = CRITIC_PROMPT.format(
                audience=self.config.audience,
                presentation_length=self.config.presentation_length,
                commitment_md=commitment_md,
                merged_slides_json=json.dumps(merged_slides, ensure_ascii=False, indent=2),
                section_inventory=json.dumps(section_order, ensure_ascii=False, indent=2),
                paragraph_snippets=json.dumps(snippets, ensure_ascii=False, indent=2),
            )
        else:
            prompt = CRITIC_PROMPT_NO_COMMITMENT.format(
                audience=self.config.audience,
                presentation_length=self.config.presentation_length,
                merged_slides_json=json.dumps(merged_slides, ensure_ascii=False, indent=2),
                section_inventory=json.dumps(section_order, ensure_ascii=False, indent=2),
                paragraph_snippets=json.dumps(snippets, ensure_ascii=False, indent=2),
            )

        if self.critic_config.save_intermediate and self.config.save_intermediate:
            (self.output_dir / "narrative_critic_prompt.txt").write_text(prompt, encoding="utf-8")

        raw = self.provider.generate(prompt)
        if raw is None:
            return {
                "success": False,
                "error": "LLM provider returned None",
                "commentary": None,
                "commentary_path": None,
            }

        raw_text = json.dumps(raw, ensure_ascii=False, indent=2) if isinstance(raw, (dict, list)) else str(raw)

        if self.critic_config.save_intermediate and self.config.save_intermediate:
            (self.output_dir / "narrative_critic_raw_response.txt").write_text(raw_text, encoding="utf-8")

        try:
            parsed = extract_json_from_response(raw_text)
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to parse JSON from LLM response: {e}",
                "commentary": None,
                "commentary_path": None,
            }

        out_path = self.output_dir / f"narrative_critique_round_{round_number}.json"
        out_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "success": True,
            "commentary": parsed,
            "commentary_path": str(out_path),
        }
