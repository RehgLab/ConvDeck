"""
Per-reviser tool-call statistics for the ReSpAct feedback revisers.

Both feedback revisers (the outline reviser and the slide-plan reviser) run a
ReSpAct Think/Speak/Act loop. This module accumulates *session totals* of how
many tool calls each reviser made, broken down by category, and writes them
into a single shared per-paper JSON.

Usage (one instance per reviser session)::

    stats = ToolCallStats(mode="simulated")
    # ... per round:
    stats.start_round()
    # ... per ReSpAct turn:
    stats.record_turn(has_think=bool(thought_text))
    stats.record_tool("apply_edits")
    stats.record_apply_edits(op_results)   # the engine's OpResult list
    # ... at session end:
    stats.write(json_path, "outline_reviser", paper="open_vocab")

The JSON has two top-level reviser sections; each reviser writes only its own
section, so a full pipeline run ends with both populated::

    {
      "paper": "open_vocab",
      "outline_reviser":    { "mode": ..., "rounds": ..., ... },
      "slide_plan_reviser": { "mode": ..., "rounds": ..., ... }
    }
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

# The two reviser section keys in the shared JSON.
OUTLINE_SECTION = "outline_reviser"
SLIDE_PLAN_SECTION = "slide_plan_reviser"

# ask_user is the only SPEAK tool; every other tool is an ACT.
_SPEAK_TOOLS = {"ask_user"}
# Tools that are ACTs but not counted as "unknown".
_KNOWN_ACT_TOOLS = {"arxiv_search", "web_search", "apply_edits", "finalize", "finish"}


class ToolCallStats:
    """Accumulates tool-call counts for one reviser session."""

    def __init__(self, mode: str = "") -> None:
        self.mode = mode               # "interactive" | "simulated"
        self.rounds = 0
        self.turns = 0
        self.think_turns = 0
        self.act = 0
        self.speak = 0
        self.arxiv_search = 0
        self.web_search = 0
        self.apply_edits_calls = 0
        self.finalize_calls = 0
        self.finish_calls = 0
        self.unknown_tool = 0
        self.oneshot_calls = 0
        self.sub_ops_total = 0
        self.sub_ops_applied = 0
        self.sub_ops_failed = 0
        self._applied_by_type: Counter = Counter()
        self._failed_by_type: Counter = Counter()

    # ── recording ───────────────────────────────────────────────────────────

    def start_round(self) -> None:
        """Mark the start of one revision round."""
        self.rounds += 1

    def record_turn(self, has_think: bool) -> None:
        """Record one ReSpAct loop turn (one model call)."""
        self.turns += 1
        if has_think:
            self.think_turns += 1

    def record_tool(self, name: str) -> None:
        """Count one tool call by name.

        ``ask_user`` counts as a SPEAK; everything else as an ACT. The
        sub-edit-ops inside an ``apply_edits`` call are recorded separately
        via :meth:`record_apply_edits`.
        """
        name = name or ""
        if name in _SPEAK_TOOLS:
            self.speak += 1
        else:
            self.act += 1
        if name == "arxiv_search":
            self.arxiv_search += 1
        elif name == "web_search":
            self.web_search += 1
        elif name == "apply_edits":
            self.apply_edits_calls += 1
        elif name == "finalize":
            self.finalize_calls += 1
        elif name == "finish":
            self.finish_calls += 1
        elif name not in _SPEAK_TOOLS and name not in _KNOWN_ACT_TOOLS:
            self.unknown_tool += 1

    def record_apply_edits(self, op_results: List[Any]) -> None:
        """Record the sub-edit-ops of one ``apply_edits`` call.

        ``op_results`` is the list of ``edit_ops.OpResult`` objects the engine
        returned — each has ``.op`` (op type) and ``.ok``.
        """
        for r in op_results or []:
            self.sub_ops_total += 1
            op = getattr(r, "op", "") or "unknown"
            if getattr(r, "ok", False):
                self.sub_ops_applied += 1
                self._applied_by_type[op] += 1
            else:
                self.sub_ops_failed += 1
                self._failed_by_type[op] += 1

    def record_oneshot(self) -> None:
        """Record a non-GPT-5 one-shot revision (whole-artifact regeneration,
        no ReSpAct tool loop)."""
        self.oneshot_calls += 1

    # ── output ──────────────────────────────────────────────────────────────

    def to_section(self) -> Dict[str, Any]:
        """Render this session's counters as one reviser section."""
        types = sorted(set(self._applied_by_type) | set(self._failed_by_type))
        return {
            "mode": self.mode,
            "rounds": self.rounds,
            "turns": self.turns,
            "think_turns": self.think_turns,
            "act": self.act,
            "speak": self.speak,
            "arxiv_search": self.arxiv_search,
            "web_search": self.web_search,
            "apply_edits_calls": self.apply_edits_calls,
            "finalize_calls": self.finalize_calls,
            "finish_calls": self.finish_calls,
            "unknown_tool": self.unknown_tool,
            "oneshot_calls": self.oneshot_calls,
            "sub_ops_total": self.sub_ops_total,
            "sub_ops_applied": self.sub_ops_applied,
            "sub_ops_failed": self.sub_ops_failed,
            "sub_ops_by_type": {
                t: {
                    "applied": self._applied_by_type.get(t, 0),
                    "failed": self._failed_by_type.get(t, 0),
                }
                for t in types
            },
        }

    def write(self, json_path: Any, section_key: str, *, paper: str = "") -> None:
        """Merge this session's section into the shared per-paper JSON.

        ``section_key`` is :data:`OUTLINE_SECTION` or :data:`SLIDE_PLAN_SECTION`.
        The other reviser's section, if already present, is preserved. Never
        raises — a logging failure must not break a pipeline run.
        """
        try:
            path = Path(json_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data: Dict[str, Any] = {}
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8")) or {}
                except Exception:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            if paper:
                data["paper"] = paper
            data[section_key] = self.to_section()
            path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
