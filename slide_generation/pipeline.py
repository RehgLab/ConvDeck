import os
from dotenv import load_dotenv
load_dotenv()

from utils.llm.config import get_agent_config
from slide_generation.renderer.js_renderer import generate_pptx_from_plan_using_pptxgenjs
from slide_generation.outline_generation.integration import (
    run_rst_integration_pipeline,
    create_config_from_agent_config,
    run_commitment_building_pipeline,
)
from slide_generation.outline_generation.summarization import run_summarization_pipeline
from slide_generation.outline_generation.raw_content_feedback_agent_respact import (
    run_simulated_raw_content_feedback_loop,
    run_raw_content_feedback_loop,
)
from slide_generation.content_generation.feedback_js import (
    apply_simulated_feedback_js,
    apply_user_feedback_js,
)
from slide_generation.content_generation.pdf_parser import (
    gen_image_and_table,
    reformat_slides,
)
from slide_generation.content_generation.figure_matcher import (
    filter_image_table,
    gen_figure_match,
)
from slide_generation.content_generation.layout_planner import generate_slide_plan
from slide_generation.content_generation.plan_refiner import refine_slide_plan
from slide_generation.interaction_logger import (
    init_interaction_logger,
    log_user_feedback,
    close_interaction_logger,
)

import argparse
import json
import re
import time
import requests
import shutil
import glob

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

pipeline_options = PdfPipelineOptions()
pipeline_options.images_scale = 5.0
pipeline_options.generate_page_images = True
pipeline_options.generate_picture_images = True

doc_converter = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
    }
)

_lo_programs = glob.glob(os.path.expanduser("~/libreoffice/opt/libreoffice*/program"))
if _lo_programs:
    os.environ["PATH"] = _lo_programs[-1] + ":" + os.environ["PATH"]


def check_vllm_server(url="http://127.0.0.1:7000/v1"):
    """Check if vLLM server is running and accessible."""
    try:
        response = requests.get(f"{url}/models", timeout=5)
        return response.status_code == 200
    except Exception:
        return False


class TokenTracker:
    """Accumulates token usage and timing across pipeline steps."""

    def __init__(self):
        self.total_in = 0
        self.total_out = 0
        self.log = {}
        self.path = None

    def record(self, step, in_tok, out_tok, time_taken=None):
        self.total_in += in_tok
        self.total_out += out_tok
        self.log[f'{step}_in_t'] = in_tok
        self.log[f'{step}_out_t'] = out_tok
        if time_taken is not None:
            self.log[f'{step}_time'] = time_taken
        self.save(self.path)

    def save(self, path):
        with open(path, 'w') as f:
            json.dump(self.log, f, indent=4)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ConvDeck: PDF → Slide Generation')
    parser.add_argument('paper_path', type=str, help='Path to input PDF')
    parser.add_argument('--model', type=str, default='gpt-5', help='Model name (default: gpt-5)')
    parser.add_argument('--template', type=int, default=3, choices=range(1, 9),
                        help='Slide template theme 1-8 (default: 3)')
    parser.add_argument('--duration', type=int, default=20,
                        help='Target presentation length in minutes (default: 20)')
    parser.add_argument('--audience', type=str, default='researchers',
                        help='Target audience (default: researchers)')
    parser.add_argument('--no_outline_feedback', dest='outline_feedback', action='store_false',
                        help='Disable simulated feedback on the outline during RST integration (on by default)')
    parser.add_argument('--no_llm_feedback', dest='llm_feedback', action='store_false',
                        help='Disable the LLM/VLM-simulated feedback loop on the outline/RST content (on by default)')
    parser.add_argument('--no_use_respact', dest='use_respact', action='store_false',
                        help='Disable the ReSpAct reviser for the content feedback loop (on by default)')
    parser.add_argument('--no_use_edit_ops', dest='use_edit_ops', action='store_false',
                        help='Reviser regenerates the whole slide list instead of emitting localized edit operations (edit-ops on by default)')
    parser.add_argument('--no_js_feedback', dest='js_feedback', action='store_false',
                        help='Disable simulated feedback on the generated JS deck (on by default)')
    parser.add_argument('--interactive', action='store_true',
                        help='Run the conversational feedback stages (3 & 5) as an interactive '
                             'human session — the pipeline blocks on input() for real-user feedback '
                             'and clarifying answers instead of the LLM/VLM simulator. This is the '
                             'mode the study web app (app.py) drives.')
    parser.add_argument('--summarize', type=lambda v: str(v).lower() not in ('0', 'false', 'no', 'off'),
                        default=True,
                        help='Condense the extracted paper markdown into processed.md before '
                             'outline generation (default: true). Pass --summarize false to feed '
                             'the full, non-summarized markdown to every downstream agent.')
    parser.add_argument('--js_theme', type=int, default=0, choices=(0, 4, 6, 7),
                        help='PptxGenJS color theme (0,4,6,7) (default: 0)')
    parser.add_argument('--js_design', type=int, default=5, choices=range(0, 6),
                        help='PptxGenJS design layout 0-5 (default: 5)')
    parser.add_argument('--no_log_interactions', dest='log_interactions', action='store_false',
                        help='Disable logging of agent interactions (input/output/tokens) and user '
                             'feedback to contents/{paper_name}/{prefix}_interaction_log.jsonl '
                             '(logging on by default)')
    parser.add_argument('--user_instructions', type=str, default=None,
                        help='Global instructions for slide generation. Embedded into the '
                             'commitment document and used throughout the pipeline. '
                             'Pass an empty string (or omit) to skip.')
    parser.add_argument('--paper_name', type=str, default=None,)
    args = parser.parse_args()

    # Derive internal fields
    args.paper_name = args.paper_name or os.path.splitext(os.path.basename(args.paper_path))[0].replace(' ', '_')
    args.model_name_t = args.model
    args.model_name_v = args.model
    args.formula_mode = 1

    start_time = time.time()
    os.makedirs('tmp', exist_ok=True)
    os.makedirs(f'contents/{args.paper_name}', exist_ok=True)

    tracker = TokenTracker()
    agent_config = get_agent_config(args.model)
    prefix = f'<{args.model}_{args.model}>'
    tracker.path = f'contents/{args.paper_name}/{prefix}_log.json'

    if args.log_interactions:
        init_interaction_logger(
            f'contents/{args.paper_name}/{prefix}_interaction_log.jsonl'
        )

    rst_config = create_config_from_agent_config(
        agent_config,
        output_dir='rst_outputs',
        presentation_length=args.duration,
        target_audience=args.audience,
    )

    # Check vLLM server if needed
    platform_str = str(agent_config.get('model_platform', ''))
    if 'vllm' in platform_str.lower():
        vllm_url = agent_config.get('url', 'http://127.0.0.1:8000/v1')
        if not check_vllm_server(vllm_url):
            print(f"Warning: vLLM server at {vllm_url} not reachable, continuing anyway...")

    # ── Step 1: PDF → Docling conversion ─────────────────────────────────
    raw_result = doc_converter.convert(args.paper_path)
    raw_markdown = raw_result.document.export_to_markdown()
    text_content = re.compile(r"<!--[\s\S]*?-->").sub("", raw_markdown)

    if len(text_content) < 500:
        print('\nDocling extraction too short, falling back to marker\n')
        from utils.core.model_utils import parse_pdf
        import torch
        from marker.models import create_model_dict
        parser_model = create_model_dict(device='cuda', dtype=torch.float16)
        input("Parsing pdf")
        text_content, _rendered = parse_pdf(args.paper_path, model_lst=parser_model, save_file=False)
        text_content = re.compile(r"<!--[\s\S]*?-->").sub("", text_content)

    text_content = re.sub(r'## References[\s\S]*$', '', text_content)  # Remove references and below.

    # The extracted paper markdown, before any processing. Always (re)written.
    initial_md_path = f'contents/{args.paper_name}/{prefix}_initial_markdown.md'
    with open(initial_md_path, "w", encoding="utf-8") as f:
        f.write(text_content)

    # ``processed.md`` is what every downstream agent consumes. By default it
    # is the summarized markdown; with --summarize false it is a verbatim copy
    # of the initial markdown. Cached: if it already exists it is reused as-is
    # (delete it to re-run this stage).
    processed_md_path = f'contents/{args.paper_name}/{prefix}_processed.md'
    if os.path.exists(processed_md_path):
        text_content = open(processed_md_path, "r", encoding="utf-8").read()
        print(f"Loaded cached processed markdown from {processed_md_path}")
    else:
        if args.summarize:
            text_content, in_t, out_t = run_summarization_pipeline(text_content, rst_config)
            tracker.record('summarization', in_t, out_t)
            print("Summarized initial markdown into processed markdown")
        else:
            print("Summarization disabled (--summarize false); "
                  "using the initial markdown as processed markdown")
        with open(processed_md_path, "w", encoding="utf-8") as f:
            f.write(text_content)
        print(f"Saved processed markdown to {processed_md_path}")

    # ── Step 1.5: Global user instructions ─────────────────────────────
    instructions_cache = f'contents/{args.paper_name}/{prefix}_user_instructions.txt'
    if args.user_instructions is not None:
        args.global_user_instructions = args.user_instructions
    elif os.path.exists(instructions_cache):
        args.global_user_instructions = open(instructions_cache, "r", encoding="utf-8").read().strip()
        print(f"Loaded cached user instructions from {instructions_cache}")
    else:
        args.global_user_instructions = ""

    if args.global_user_instructions and not os.path.exists(instructions_cache):
        with open(instructions_cache, "w", encoding="utf-8") as f:
            f.write(args.global_user_instructions)
        print(f"Saved user instructions to {instructions_cache}")

    if args.global_user_instructions:
        log_user_feedback(
            stage="global_user_instructions",
            content=args.global_user_instructions,
        )

    # ── Step 2: Discourse parsing + slide grouping ───────────────────────
    commitment_cache = f'contents/{args.paper_name}/{prefix}_commitment.md'
    if not os.path.exists(commitment_cache):
        commitment_md, in_t, out_t = run_commitment_building_pipeline(
            text_content, rst_config,
            poster_name=args.paper_name,
            target_audience=args.audience,
            user_instructions=args.global_user_instructions,
        )
        tracker.record('commitment', in_t, out_t)
        with open(commitment_cache, "w", encoding="utf-8") as f:
            f.write(commitment_md)
        print(f"Saved commitment to {commitment_cache}")
    else:
        commitment_md = open(commitment_cache, "r", encoding="utf-8").read()
        print(f"Loaded cached commitment from {commitment_cache}")

    rst_cache = f'contents/{args.paper_name}/{prefix}_raw_content_rst.json'
    if not os.path.exists(rst_cache):
        slide_content, outline_tokens = run_rst_integration_pipeline(
            args, rst_config,
            markdown_content=text_content,
            poster_name=args.paper_name,
            commitment_md=commitment_md,
            use_commitment_building=True,
            target_audience=args.audience,
            outline_feedback=args.outline_feedback,
            simulate_feedback=True,
        )

        for agent_name, (in_t, out_t) in outline_tokens.items():
            tracker.record(agent_name, in_t, out_t)
    else:
        slide_content = json.load(open(rst_cache, 'r'))
        print(f'Loaded cached RST content from {rst_cache}')

    # ── Step 2.5: feedback on outline/RST integration ────────────────────
    if args.interactive:
        shutil.copy(rst_cache, f'contents/{args.paper_name}/{prefix}_raw_content_before_feedback.json')
        print("\nRunning interactive (human) feedback on outline/RST integration...")
        slide_content, fb_in, fb_out, fb_breakdown = run_raw_content_feedback_loop(
            slide_content, args,
        )
        for _step, _d in fb_breakdown.items():
            tracker.record(_step, _d['in'], _d['out'], _d['time'])
        json.dump(slide_content, open(rst_cache, 'w'), indent=4)
    elif args.use_respact and args.llm_feedback:
        shutil.copy(rst_cache, f'contents/{args.paper_name}/{prefix}_raw_content_before_feedback.json')
        print("\nRunning LLM/VLM-simulated feedback on outline/RST integration...")
        slide_content, fb_in, fb_out, fb_breakdown = run_simulated_raw_content_feedback_loop(
            slide_content, text_content, args,
        )
        for _step, _d in fb_breakdown.items():
            tracker.record(_step, _d['in'], _d['out'], _d['time'])
        json.dump(slide_content, open(rst_cache, 'w'), indent=4)

    # ── Step 3: Visual extraction + slide-plan generation ────────────────
    # The figure/layout chain expects the outline as a nested
    # {sections:[{subsections:[...]}]} structure under <prefix>_raw_content.json,
    # so convert the (feedback-revised) flat RST slide list and persist it.
    json.dump(
        reformat_slides(slide_content),
        open(f'contents/{args.paper_name}/{prefix}_raw_content.json', 'w'),
        indent=4,
    )

    # Extract images/tables from the PDF, filter them, and match figures to slides.
    gen_image_and_table(args, raw_result)
    in_t, out_t = filter_image_table(args, agent_config)
    tracker.record('filter', in_t, out_t)
    in_t, out_t, dt, _figures = gen_figure_match(args, agent_config, raw_result)
    tracker.record('mapper', in_t, out_t, dt)

    # Layout agent: assign slide templates and figure placements → slide_plan.json.
    in_t, out_t, dt = generate_slide_plan(args)
    tracker.record('arranger', in_t, out_t, dt)
    in_t, out_t, dt = refine_slide_plan(args)
    tracker.record('refiner', in_t, out_t, dt)

    # ── Step 4: PptxGenJS rendering ──────────────────────────────────────
    print("\nGenerating pptx via PptxGenJS...")
    generate_pptx_from_plan_using_pptxgenjs(
        args, bullet_font_size=16, title_font_size=24,
        theme_id=args.js_theme, design_id=args.js_design,
        use_animation=False, dynamically_adjust_layout=True,
    )
    js_file = f"contents/{args.paper_name}/{args.model}_{args.model}_pptxgenjs_theme{args.js_theme}_design{args.js_design}.js"
    os.system(f"node {js_file}")

    # ── Step 3.5: feedback on the JS deck ────────────────────────────────
    if args.interactive:
        shutil.copy(js_file, f'contents/{args.paper_name}/{prefix}_pptxgenjs_before_feedback.js')
        print("\nRunning interactive (human) feedback on JS deck...")
        in_t, out_t, dt, fb_breakdown = apply_user_feedback_js(args, js_file)
        for _step, _d in fb_breakdown.items():
            tracker.record(_step, _d['in'], _d['out'], _d['time'])
    elif args.js_feedback:
        shutil.copy(js_file, f'contents/{args.paper_name}/{prefix}_pptxgenjs_before_feedback.js')
        print("\nRunning LLM/VLM-simulated feedback on JS deck...")
        in_t, out_t, dt, fb_breakdown = apply_simulated_feedback_js(args, js_file)
        for _step, _d in fb_breakdown.items():
            tracker.record(_step, _d['in'], _d['out'], _d['time'])

    print(f"Pipeline time: {time.time() - start_time:.1f}s")
    tracker.save(tracker.path)
    close_interaction_logger()
    print("Done.")
