"""
Refiner agent: run after generate_slide_plan.

- Adds a figure to slides that have no image/table (when one fits), without changing
  slides that already have figures/tables.
- Refines bullets: 3–6 per slide; if fewer than 3 expand from raw_content; if more than 6 reduce.
- For text-only slides, adds sub-bullets using raw_content.
- Applies LaTeX formatting: \\textbf{}, \\textcolor{red}{}, \\textcolor{blue}{} per layout rules.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml
from jinja2 import Environment, StrictUndefined
from openai import OpenAI

from utils.core.helpers import get_json_from_response
from utils.llm.config import get_agent_config
from utils.llm.chat import account_token
from utils.llm.chat import extract_text_from_responses

from camel.models import ModelFactory
from camel.agents import ChatAgent


def _load_refiner_inputs(args) -> Tuple[Dict, Dict, Dict]:
    """Load slide_plan, raw_content, and images_filtered. Return (slide_plan, raw_content, available_images)."""
    prefix = f"<{args.model_name_t}_{args.model_name_v}>"
    content_dir = Path(f"contents/{args.paper_name}")
    images_dir = Path(f"{prefix}_images_and_tables/{args.paper_name}")

    slide_plan_path = content_dir / f"{prefix}_slide_plan.json"
    raw_content_path = content_dir / f"{prefix}_raw_content.json"
    images_filtered_path = images_dir / "images_filtered.json"

    if not slide_plan_path.is_file():
        raise FileNotFoundError(f"Slide plan not found: {slide_plan_path}")
    if not raw_content_path.is_file():
        raise FileNotFoundError(f"Raw content not found: {raw_content_path}")
    if not images_filtered_path.is_file():
        raise FileNotFoundError(f"Images filtered not found: {images_filtered_path}")

    slide_plan = json.loads(slide_plan_path.read_text(encoding="utf-8"))
    raw_content = json.loads(raw_content_path.read_text(encoding="utf-8"))
    images_filtered = json.loads(images_filtered_path.read_text(encoding="utf-8"))

    # Build available_images: list of {filename, caption [, width, height]} for prompt
    available_images = {}
    for filename, meta in images_filtered.items():
        if not isinstance(meta, dict):
            continue
        available_images[filename] = {
            "caption": meta.get("caption", ""),
            "width": meta.get("width"),
            "height": meta.get("height"),
        }
    return slide_plan, raw_content, available_images


def refine_slide_plan(args, use_refiner_model: str | None = None) -> Tuple[int, int, float]:
    """
    Refine the slide plan: add figures to text-only slides when appropriate,
    refine bullets (3–6, sub-bullets for text-only), apply LaTeX formatting.
    Leaves slides that already have images/tables unchanged.

    Returns:
        (input_tokens, output_tokens, time_taken)
    """
    model_name = use_refiner_model or getattr(args, "model_name_v", None) or args.model_name_t
    slide_plan, raw_content, available_images = _load_refiner_inputs(args)

    prompt_dir = Path("prompts/pipeline")
    prompt_path = prompt_dir / "refine_slide_plan.yaml"
    if not prompt_path.is_file():
        raise FileNotFoundError(f"Refiner prompt not found: {prompt_path}")
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt_cfg = yaml.safe_load(f)

    jinja_env = Environment(undefined=StrictUndefined)
    template = jinja_env.from_string(prompt_cfg["template"])
    planner_prompt = template.render(
        slide_plan_json=json.dumps(slide_plan, indent=2),
        raw_content_json=json.dumps(raw_content, indent=2),
        available_images_json=json.dumps(available_images, indent=2),
    )

    cfg = get_agent_config(model_name)
    use_gpt5 = "gpt-5" in getattr(args, "model_name_t", "").lower()
    start_time = time.time()

    if use_gpt5:
        client = OpenAI()
        response = client.responses.create(
            model=model_name,
            input=planner_prompt,
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"},
        )
        raw_text = extract_text_from_responses(response)
        in_tok = getattr(getattr(response, "usage", None), "input_tokens", None) or 0
        out_tok = getattr(getattr(response, "usage", None), "output_tokens", None) or 0
    else:
        if getattr(args, "model_name_t", "").startswith("vllm_qwen"):
            from utils.llm.chat import chat_via_vllm
            vllm_model = ModelFactory.create(
                model_platform=cfg["model_platform"],
                model_type=cfg["model_type"],
                model_config_dict=cfg["model_config"],
                url=cfg.get("url"),
            )
            response = chat_via_vllm(planner_prompt, cfg, vllm_model, prompt_cfg["system_prompt"])
            raw_text = response.choices[0].message.content
            in_tok = response.usage.prompt_tokens
            out_tok = response.usage.completion_tokens
        else:
            model = ModelFactory.create(
                model_platform=cfg["model_platform"],
                model_type=cfg["model_type"],
                model_config_dict=cfg["model_config"],
                url=cfg.get("url"),
            )
            agent = ChatAgent(
                system_message=prompt_cfg["system_prompt"],
                model=model,
                message_window_size=5,
            )
            agent.reset()
            response = agent.step(planner_prompt)
            raw_text = response.msgs[0].content
            in_tok, out_tok = account_token(response)

    time_taken = time.time() - start_time
    refined = get_json_from_response(raw_text)

    if "metadata" not in refined or "slides" not in refined:
        raise ValueError("Refiner output must contain 'metadata' and 'slides'.")

    # Preserve slides that had images/tables: refiner might have changed them; re-enforce from original
    original_slides = slide_plan.get("slides", [])
    refined_slides = refined.get("slides", [])
    if len(original_slides) != len(refined_slides):
        print(f"[refine_slide_plan] Warning: slide count changed {len(original_slides)} -> {len(refined_slides)}; using refined count.")
    for i, orig in enumerate(original_slides):
        if i >= len(refined_slides):
            break
        # If original had images or tables, restore them so we never overwrite
        if orig.get("images") or orig.get("tables"):
            refined_slides[i]["images"] = list(orig.get("images", []))
            refined_slides[i]["tables"] = list(orig.get("tables", []))
            refined_slides[i]["template_id"] = orig.get("template_id", refined_slides[i].get("template_id"))
        # T19_2Text is two-column text only: never add figures
        if orig.get("template_id") == "T19_2Text":
            refined_slides[i]["images"] = []
            refined_slides[i]["tables"] = []
            refined_slides[i]["template_id"] = "T19_2Text"

    refined["slides"] = refined_slides
    out_path = Path(f"contents/{args.paper_name}/<{args.model_name_t}_{args.model_name_v}>_slide_plan.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(refined, f, indent=4)
    print(f"[refine_slide_plan] Wrote refined plan to {out_path}; tokens in={in_tok} out={out_tok}; time={time_taken:.2f}s")
    return in_tok, out_tok, time_taken


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Refine slide plan: add figures to text-only slides, refine bullets, LaTeX formatting.")
    p.add_argument("--paper_name", required=True, help="Paper name (content dir)")
    p.add_argument("--model_name_t", default="4o", help="Model name (for paths)")
    p.add_argument("--model_name_v", default="4o", help="Model name for refiner LLM")
    p.add_argument("--formula_mode", type=int, default=0)
    args = p.parse_args()
    refine_slide_plan(args)
    print("Done.")
