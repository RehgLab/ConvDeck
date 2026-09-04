"""
LLM/VLM-simulated feedback for the outline and JS-deck review stages.

Entry points (each mirrors the shape of human feedback so it drops into the
pipeline with minimal plumbing):

  • ``simulate_outline_feedback`` — text LLM that reviews the slide OUTLINE
    (raw_content_rst list) against the summarized paper and returns either
    ``None`` (approval) or a free-text feedback string for the reviser.

  • ``simulate_js_feedback`` — VLM that reviews the FINAL rendered deck
    (post-Node .pptx) and returns either ``None`` (approval) or a free-text
    feedback string for the JS edit loop.

This module only handles prompt rendering, LLM dispatch, and response parsing.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import subprocess
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from jinja2 import Environment, StrictUndefined
from openai import OpenAI
from PIL import Image

from camel.models import ModelFactory
from camel.agents import ChatAgent

from utils.llm.config import get_agent_config
from utils.llm.chat import (
    account_token,
    chat_via_vllm,
    extract_text_from_responses,
)


OUTLINE_PROMPT_PATH = Path("prompts/pipeline/simulate_outline_feedback.yaml")
JS_SIMULATE_FEEDBACK_PROMPT_PATH = Path("prompts/pipeline/simulate_js_feedback.yaml")

MAX_SIMULATED_ROUNDS = 1
MAX_SIMULATED_JS_ROUNDS = 1
_THUMB_MAX_SIDE = 512
_THUMB_QUALITY = 60


def _load_prompt(path: Path) -> Dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"Simulator prompt not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_summarized_paper(args) -> str:
    """Return the cached processed paper markdown saved by the pipeline.

    This is the summarized markdown by default, or the full (non-summarized)
    markdown when the pipeline was run with --summarize false.
    """
    prefix = f"<{args.model_name_t}_{args.model_name_v}>"
    cache = Path(f"contents/{args.paper_name}/{prefix}_processed.md")
    if not cache.is_file():
        raise FileNotFoundError(
            f"Processed paper markdown not found at {cache}; "
            "run the markdown-extraction step before LLM feedback simulation."
        )
    return cache.read_text(encoding="utf-8")


def _dispatch_llm(
    args,
    system_prompt: str,
    user_prompt: str,
    images: Optional[List[bytes]] = None,
) -> Tuple[str, int, int]:
    """Send a single-turn request; returns (raw_text, in_tok, out_tok).

    When *images* is provided and the model supports vision, the images are
    attached as input_image blocks (gpt-5 Responses API only). Other backends
    fall back to text-only.
    """
    model_name = getattr(args, "model_name_v", None) or getattr(args, "model", "4o")
    cfg = get_agent_config(model_name)
    use_gpt5 = "gpt-5" in getattr(args, "model_name_t", "").lower()

    if use_gpt5:
        client = OpenAI()
        if images:
            blocks: List[Dict[str, Any]] = [{"type": "input_text", "text": user_prompt}]
            for img_bytes in images:
                b64 = base64.b64encode(img_bytes).decode()
                blocks.append({
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{b64}",
                })
            payload: Any = [{"role": "user", "content": blocks}]
        else:
            payload = user_prompt
        response = client.responses.create(
            model=model_name,
            input=payload,
            instructions=system_prompt,
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"},
        )
        raw_text = extract_text_from_responses(response)
        in_tok = getattr(getattr(response, "usage", None), "input_tokens", 0) or 0
        out_tok = getattr(getattr(response, "usage", None), "output_tokens", 0) or 0
        return raw_text, in_tok, out_tok

    if getattr(args, "model_name_t", "").startswith("vllm_qwen"):
        model = ModelFactory.create(
            model_platform=cfg["model_platform"],
            model_type=cfg["model_type"],
            model_config_dict=cfg["model_config"],
            url=cfg.get("url"),
        )
        response = chat_via_vllm(user_prompt, cfg, model, system_prompt)
        raw_text = response.choices[0].message.content
        return raw_text, response.usage.prompt_tokens, response.usage.completion_tokens

    model = ModelFactory.create(
        model_platform=cfg["model_platform"],
        model_type=cfg["model_type"],
        model_config_dict=cfg["model_config"],
        url=cfg.get("url"),
    )
    agent = ChatAgent(
        system_message=system_prompt,
        model=model,
        message_window_size=5,
    )
    agent.reset()
    response = agent.step(user_prompt)
    raw_text = response.msgs[0].content
    in_tok, out_tok = account_token(response)
    return raw_text, in_tok, out_tok


def _parse_verdict(raw_text: str) -> Tuple[Optional[str], str]:
    """
    Parse the reviewer response.

    Expected outputs:
      - "Ready." when the reviewer approves
      - free-text feedback when revision is needed

    Returns ``(feedback_or_none, status_str)``.
    ``feedback_or_none`` is None when the reviewer approved;
    otherwise it is the free-text revision request.
    """
    text = (raw_text or "").strip()

    if not text:
        print("[llm_feedback] Empty reviewer response; treating as approval.")
        return None, "empty_response"

    normalized = text.lower().rstrip(".! \n\t")

    if normalized == "ready":
        return None, "ok"

    return text, "revise"


def _format_prior_feedback(prior_feedback: Optional[List[str]]) -> str:
    """Render the reviewer's own earlier-round feedback as an appendix block.

    Returns "" when there is no prior feedback, so callers can append the
    result unconditionally. The block makes explicit that the slide
    outline/plan shown above is the revised result of these earlier asks.
    """
    items = [str(f).strip() for f in (prior_feedback or []) if str(f).strip()]
    if not items:
        return ""
    lines = [
        "",
        "--- YOUR PRIOR FEEDBACK (earlier review rounds) ---",
        "The slide outline/plan above is the revised result of these requests",
        "you made in earlier review rounds:",
    ]
    for i, fb in enumerate(items, 1):
        lines.append(f"  Round {i}: {fb}")
    lines += [
        "Guidance: do NOT repeat points already addressed; if an earlier point",
        "is still unaddressed you may restate it (and say so explicitly); do not",
        "contradict your earlier feedback.",
        "--------------------------------------------------",
    ]
    return "\n".join(lines)


# ── Outline feedback (text LLM) ─────────────────────────────────────────────

def _render_raw_content_for_review(slides: List[Dict[str, Any]]) -> str:
    """
    Render a raw_content_rst slide list (each slide has ``title``,
    ``content``, ``discussion_idea``) as title + discussion_idea bullets.
    Mirrors the user-facing format used by the human-feedback agent.
    """
    lines = []
    for i, s in enumerate(slides, 1):
        title = (s.get("title") or "").strip() or "(untitled)"
        idea = (s.get("discussion_idea") or "").strip()
        lines.append(f"[{i}] {title}")
        if idea:
            lines.append(f"    idea: {idea}")
    return "\n".join(lines)


def simulate_outline_feedback(
    slide_content: List[Dict[str, Any]],
    paper_summary: str,
    args,
    *,
    round_number: int = 1,
    prior_feedback: Optional[List[str]] = None,
) -> Tuple[Optional[str], int, int, float]:
    """
    Outline reviewer: judges a ``raw_content_rst`` slide list (title /
    content / discussion_idea per slide) against the summarized paper.

    The downstream reviser has no fixed paragraph pool — it can rewrite, add,
    drop, or pull external arXiv material — so this prompt permits a broad set
    of edit requests.

    ``round_number`` is rendered into the prompt: round 1 is treated as a
    strict first review (the model MUST find at least one improvement unless
    the outline is exceptionally polished), later rounds are more lenient.

    Returns ``(feedback_or_none, in_tok, out_tok, dt)``. ``feedback_or_none``
    is None when the reviewer approves the outline; otherwise it is a
    free-form instruction string suitable for feeding directly into the
    human-feedback agent's ``_run_one_revision``.
    """
    prompt_cfg = _load_prompt(OUTLINE_PROMPT_PATH)
    jinja_env = Environment(undefined=StrictUndefined)
    template = jinja_env.from_string(prompt_cfg["template"])
    rendered = template.render(
        paper_summary=paper_summary,
        slide_outline=_render_raw_content_for_review(slide_content),
        round_number=round_number,
        target_audience=args.audience,
        presentation_duration=args.duration,
    )
    rendered += _format_prior_feedback(prior_feedback)

    start = time.time()
    raw_text, in_tok, out_tok = _dispatch_llm(
        args,
        system_prompt=prompt_cfg.get("system_prompt", ""),
        user_prompt=rendered,
    )
    dt = time.time() - start

    feedback, status = _parse_verdict(raw_text)
    preview = (feedback or "").replace("\n", " ")[:140]
    print(
        f"[llm_feedback] outline reviewer: round={round_number} status={status}  "
        f"tokens in={in_tok} out={out_tok}  time={dt:.2f}s"
        + (f"  feedback=\"{preview}{'…' if feedback and len(feedback) > 140 else ''}\"" if feedback else "")
    )
    return feedback, in_tok, out_tok, dt


# ── Plan rendering helpers (shared by JS feedback) ──────────────────────────

def _collect_plan_figure_thumbs(
    args,
    slide_plan: Dict[str, Any],
    max_images: int = 12,
) -> List[Dict[str, Any]]:
    """
    Collect per-slide figure thumbnails so the reviewer sees figures keyed by
    slide index (as a human would on a rendered slide), not by filename.
    Returns a list of ``{"slide_index": int, "b64": str}``.
    """
    prefix = f"<{args.model_name_t}_{args.model_name_v}>"
    img_dir = Path(f"{prefix}_images_and_tables/{args.paper_name}")
    filtered_path = img_dir / "images_filtered.json"
    tables_path = img_dir / "tables_filtered.json"

    meta: Dict[str, Dict[str, Any]] = {}
    for p in (filtered_path, tables_path):
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    meta.update({k: v for k, v in data.items() if isinstance(v, dict)})
            except Exception as exc:
                print(f"[llm_feedback] Could not read {p}: {exc}")

    def _path_for(name: str) -> Optional[str]:
        for v in meta.values():
            path = (v or {}).get("image_path") or (v or {}).get("path") or ""
            if path and Path(path).name == name:
                return path
        candidate = img_dir / name
        return str(candidate) if candidate.is_file() else None

    records: List[Dict[str, Any]] = []
    for idx, slide in enumerate(slide_plan.get("slides", []), 1):
        if len(records) >= max_images:
            break
        assets = list(slide.get("images") or []) + list(slide.get("tables") or [])
        if not assets:
            continue
        name = Path(assets[0]).name
        path = _path_for(name)
        if not path:
            continue
        try:
            img = Image.open(path).convert("RGB")
            img.thumbnail((_THUMB_MAX_SIDE, _THUMB_MAX_SIDE))
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=_THUMB_QUALITY)
            b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:
            print(f"[llm_feedback] Could not encode {path}: {exc}")
            continue
        records.append({"slide_index": idx, "b64": b64})

    return records


def _format_plan_text(slide_plan: Dict[str, Any]) -> str:
    """Compact text rendering of the full slide plan for the reviewer."""
    lines: List[str] = []
    meta = slide_plan.get("metadata", {})
    title = meta.get("title", "Untitled")
    lines.append(f'=== Slide Plan: "{title}" ===')
    lines.append("")

    def _emit_bullets(bullets, indent="  "):
        for bullet in bullets or []:
            text = bullet if isinstance(bullet, str) else bullet.get("text", "")
            lines.append(f"{indent}• {text}")
            if isinstance(bullet, dict):
                for sub in bullet.get("sub", []) or []:
                    lines.append(f"{indent}    – {sub}")

    for idx, slide in enumerate(slide_plan.get("slides", []), 1):
        section = slide.get("section", "")
        subsection = slide.get("subsection", "")
        template = slide.get("template_id", "")
        columns = slide.get("columns") or []
        if columns and not subsection:
            col_titles = " | ".join(f'"{c.get("subsection","")}"' for c in columns)
            lines.append(f"Slide {idx} — {section} / {col_titles}  [{template}] (two-column)")
        else:
            lines.append(f"Slide {idx} — {section} / \"{subsection}\"  [{template}]")
        if columns:
            for ci, col in enumerate(columns, 1):
                col_title = col.get("subsection", "")
                lines.append(f"  Column {ci}: \"{col_title}\"")
                _emit_bullets(col.get("bullets", []), indent="    ")
                col_para = col.get("paragraph", "")
                if col_para:
                    lines.append(f"    ¶ {col_para}")
        else:
            _emit_bullets(slide.get("bullets", []))
            paragraph = slide.get("paragraph", "")
            if paragraph:
                lines.append(f"  ¶ {paragraph}")
        imgs = list(slide.get("images") or [])
        tbls = list(slide.get("tables") or [])
        if imgs:
            lines.append(f"  Images: {', '.join(Path(a).name for a in imgs)}")
        else:
            lines.append("  Images: none")
        if tbls:
            lines.append(f"  Tables: {', '.join(Path(a).name for a in tbls)}")
        else:
            lines.append("  Tables: none")
        lines.append("")

    return "\n".join(lines)


def _format_figures_block(thumbs: List[Dict[str, Any]]) -> str:
    if not thumbs:
        return "(no figures assigned in the current plan)"
    parts = []
    for t in thumbs:
        parts.append(
            f"--- Figure on Slide {t['slide_index']} ---\n"
            f"thumbnail_b64: {t['b64']}"
        )
    return "\n\n".join(parts)


# ── JS-stage feedback (post-Node render) ───────────────────────────────────

def _render_pptx_to_slide_images(
    pptx_path: str,
    max_slides: int = 20,
    dpi: int = 96,
    max_w: int = 512,
    max_h: int = 288,
    quality: int = 70,
) -> List[bytes]:
    """Convert an existing .pptx (e.g. the one produced by ``node {js_file}``)
    to JPEG images — one per slide. Returns ``[]`` on any failure.

    Defaults produce small thumbnails suited to VLM input; pass a higher
    ``dpi`` / ``max_w`` / ``max_h`` / ``quality`` for crisp on-screen review.
    """
    try:
        from pdf2image import convert_from_path  # type: ignore
    except Exception as exc:
        print(f"[llm_feedback] pdf2image unavailable: {exc}")
        return []

    if not os.path.isfile(pptx_path):
        print(f"[llm_feedback] pptx not found for js feedback: {pptx_path}")
        return []

    lo_progs = glob.glob(os.path.expanduser("~/libreoffice/opt/libreoffice*/program"))
    soffice = os.path.join(lo_progs[-1], "soffice") if lo_progs else "soffice"

    with tempfile.TemporaryDirectory() as tmp_pdf_dir, \
         tempfile.TemporaryDirectory() as ui_dir:
        cmd = [
            soffice, "--headless", "--norestore", "--nolockcheck",
            f"-env:UserInstallation=file://{ui_dir}",
            "--convert-to", "pdf", pptx_path, "--outdir", tmp_pdf_dir,
        ]
        env = os.environ.copy()
        env["LC_ALL"] = "en_US.UTF-8"
        env["LANG"] = "en_US.UTF-8"
        try:
            subprocess.run(
                cmd, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, timeout=180,
            )
        except Exception as exc:
            print(f"[llm_feedback] soffice pptx→pdf failed: {exc}")
            return []
        pdfs = [f for f in os.listdir(tmp_pdf_dir) if f.endswith(".pdf")]
        if not pdfs:
            return []
        try:
            pages = convert_from_path(os.path.join(tmp_pdf_dir, pdfs[0]), dpi=dpi)
        except Exception as exc:
            print(f"[llm_feedback] pdf2image failed: {exc}")
            return []

    out: List[bytes] = []
    for page in pages[:max_slides]:
        page.thumbnail((max_w, max_h))
        buf = BytesIO()
        page.convert("RGB").save(buf, format="JPEG", quality=quality)
        out.append(buf.getvalue())
    return out


def simulate_js_feedback(
    pptx_path: str,
    slide_plan: Dict[str, Any],
    raw_content: Any,
    args,
    round_num: int = 1,
    prior_feedback: Optional[List[str]] = None,
) -> Tuple[Optional[str], int, int, float]:
    """One round of VLM review on the FINAL rendered deck (post-Node).

    Returns ``(feedback_or_none, in_tok, out_tok, dt)``; ``feedback_or_none``
    is None when the reviewer approves.
    """
    prompt_cfg = _load_prompt(JS_SIMULATE_FEEDBACK_PROMPT_PATH)

    slide_images = _render_pptx_to_slide_images(pptx_path)
    if slide_images:
        figures_block = (
            f"{len(slide_images)} rendered slide image(s) are attached below "
            "in order (slide 1 first). Judge content AND visual issues "
            "(font size, overflow, figure placement, alignment) from these."
        )
    else:
        print("[llm_feedback] js rendering unavailable; falling back to figure thumbnails.")
        fallback_thumbs = _collect_plan_figure_thumbs(args, slide_plan)
        figures_block = _format_figures_block(fallback_thumbs)

    raw_content_str = (
        json.dumps(raw_content, indent=2)
        if not isinstance(raw_content, str)
        else raw_content
    )

    jinja_env = Environment(undefined=StrictUndefined)
    template = jinja_env.from_string(prompt_cfg["template"])
    rendered = template.render(
        raw_content=raw_content_str,
        slide_plan_text=_format_plan_text(slide_plan),
        figures_block=figures_block,
        round_num=round_num,
        is_first_round=(round_num == 1),
        target_audience=args.audience,
        presentation_duration=args.duration,
    )
    rendered += _format_prior_feedback(prior_feedback)

    start = time.time()
    raw_text, in_tok, out_tok = _dispatch_llm(
        args,
        system_prompt=prompt_cfg.get("system_prompt", ""),
        user_prompt=rendered,
        images=slide_images or None,
    )
    dt = time.time() - start

    feedback, status = _parse_verdict(raw_text)
    preview = (feedback or "").replace("\n", " ")[:140]
    print(
        f"[llm_feedback] js-deck reviewer: status={status}  "
        f"slide_imgs={len(slide_images)}  "
        f"tokens in={in_tok} out={out_tok}  time={dt:.2f}s"
        + (f"  feedback=\"{preview}{'…' if feedback and len(feedback) > 140 else ''}\"" if feedback else "")
    )
    return feedback, in_tok, out_tok, dt
