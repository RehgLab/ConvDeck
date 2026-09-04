"""
Shared slide-plan feedback helpers.

Helpers consumed by the JS-deck feedback stage (``feedback_js.py``):

  • ``_FeedbackLogger``     — plain-text session logger
  • ``_load_raw_content``   — load cached raw content (``_raw_content.json``,
    falling back to ``_raw_content_rst.json``)
  • ``_image_dir`` /
    ``_load_image_registry`` — registered figure/table filenames
  • ``format_plan_summary`` — compact human-readable slide-plan rendering
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Tuple


# ── Session logger ──────────────────────────────────────────────────────────

class _FeedbackLogger:
    def __init__(self, log_path: Path) -> None:
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            f"ConvDeck Feedback Session Log (ReSpAct)\n"
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
            if len(v_str) > 200:
                v_str = v_str[:200] + "…"
            self._append(f"    {k}: {v_str}")

    def tool_result(self, name: str, status: str, detail: str = "") -> None:
        self._append(f"[{self._ts()}] [tool_result] ← {name}: {status}")
        if detail:
            self._append(f"    {detail}")

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


# ── Artifact loaders ────────────────────────────────────────────────────────

def _load_raw_content(args) -> Dict[str, Any]:
    prefix = f"<{args.model_name_t}_{args.model_name_v}>"
    base = Path(f"contents/{args.paper_name}")
    p = base / f"{prefix}_raw_content.json"
    if not p.is_file():
        # Fall back to the RST narrative representation of the raw content.
        rst = base / f"{prefix}_raw_content_rst.json"
        if not rst.is_file():
            raise FileNotFoundError(f"Raw content not found: {p}")
        p = rst
    return json.loads(p.read_text(encoding="utf-8"))


def _image_dir(args) -> Tuple[Path, Path, Path]:
    prefix = f"<{args.model_name_t}_{args.model_name_v}>"
    output_dir = Path(f"{prefix}_images_and_tables/{args.paper_name}")
    output_dir.mkdir(parents=True, exist_ok=True)
    images_json_path = Path(f"{prefix}_images_and_tables/{args.paper_name}_images.json")
    filtered_path = output_dir / "images_filtered.json"
    if not images_json_path.is_file():
        images_json_path.write_text("{}", encoding="utf-8")
    return output_dir, images_json_path, filtered_path


def _load_image_registry(args) -> set:
    """Set of registered asset *filenames* (``Path(...).name``).

    Passed to ``apply_slide_plan_ops`` so an ``add_slide`` op cannot introduce
    a figure filename that does not exist.
    """
    _, images_json_path, _ = _image_dir(args)
    try:
        images = json.loads(images_json_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    reg: set = set()
    for rec in (images or {}).values():
        path = (rec or {}).get("image_path") or (rec or {}).get("path") or (rec or {}).get("file") or ""
        if path:
            reg.add(Path(path).name)
    return reg


# ── Slide-plan rendering ────────────────────────────────────────────────────

def format_plan_summary(slide_plan: Dict[str, Any]) -> str:
    lines: list[str] = []
    meta = slide_plan.get("metadata", {})
    title = meta.get("title", "Untitled")
    author = meta.get("author", "")
    lines.append(f'=== Slide Plan: "{title}" ===')
    if author:
        lines.append(f"Author: {author}")
    lines.append("")
    slides = slide_plan.get("slides", [])
    for idx, slide in enumerate(slides, 1):
        section = slide.get("section", "")
        template = slide.get("template_id", "")
        columns = slide.get("columns")
        if columns:
            col_titles = " | ".join(f'"{c.get("subsection", "")}"' for c in columns)
            lines.append(f"Slide {idx} — {section} / {col_titles}  [{template}]")
            for ci, col in enumerate(columns, 1):
                lines.append(f"  ── Column {ci}: \"{col.get('subsection', '')}\"")
                for bullet in col.get("bullets", []) or []:
                    text = bullet if isinstance(bullet, str) else bullet.get("text", "")
                    lines.append(f"    • {text}")
                col_para = col.get("paragraph", "") or ""
                if col_para:
                    short = col_para[:120] + ("..." if len(col_para) > 120 else "")
                    lines.append(f"    ¶ {short}")
        else:
            subsection = slide.get("subsection", "")
            lines.append(f"Slide {idx} — {section} / \"{subsection}\"  [{template}]")
            for bullet in slide.get("bullets", []):
                text = bullet if isinstance(bullet, str) else bullet.get("text", "")
                lines.append(f"  • {text}")
            paragraph = slide.get("paragraph", "")
            if paragraph:
                short = paragraph[:120] + ("..." if len(paragraph) > 120 else "")
                lines.append(f"  ¶ {short}")
        imgs = slide.get("images", [])
        tbls = slide.get("tables", [])
        assets = imgs + tbls
        if assets:
            names = ", ".join(Path(a).name for a in assets)
            lines.append(f"  Figures: {names}")
        else:
            lines.append("  Figures: [none]")
        lines.append("")
    lines.append(f"Total slides: {len(slides)}")
    return "\n".join(lines)
