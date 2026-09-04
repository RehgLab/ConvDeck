#!/usr/bin/env python3
"""
ConvDeck — User-Study Web UI (Gradio)
====================================

A polished, HuggingFace-style guided interface for the *real-user* slide
generation study.  It is a drop-in alternative front-end to ``app.py`` and
reuses the exact same backend: the :class:`PipelineSession` state machine that
drives the interactive pipeline

    python -m slide_generation.pipeline <pdf> --interactive

and walks a participant through the two interaction points the pipeline pauses
at:

    1. Outline review     → free-form feedback / approve   (loops)
    2. Slide-deck review  → free-form feedback / approve   (loops, live PNGs)

The pipeline blocks on ``input()``; the session runs it as a subprocess, reads
its stdout, recognises the prompt banners, and exposes a small state machine.
This UI polls that state machine with a 1.5 s ``gr.Timer`` and shows the right
stage, mirroring the single-page FastAPI UI in ``app_ui.py``.

Run (from the repo root, with deps installed):
    python gradio_app.py     # http://<host>:7860
"""

from __future__ import annotations

import html
import re
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

import os

# Gradio caches uploads/temp under $GRADIO_TEMP_DIR (default <tmpdir>/gradio).
# On a shared machine that default dir may already be owned by another user,
# giving "PermissionError: [Errno 13] ... '/tmp/gradio/...'". Default it to a
# per-checkout, writable dir (tmp/ is gitignored) unless the user set one.
if not os.environ.get("GRADIO_TEMP_DIR"):
    _gradio_tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", "gradio_cache")
    os.makedirs(_gradio_tmp, exist_ok=True)
    os.environ["GRADIO_TEMP_DIR"] = _gradio_tmp

import gradio as gr

# Reuse the entire UI-agnostic backend from app.py (importing does NOT start the
# FastAPI server — that lives under app.py's ``if __name__ == "__main__"``).
from app import (
    AUDIENCE_PRESETS,
    DEFAULT_MODEL,
    MODELS,
    PipelineSession,
    ROOT,
    SESSIONS_DIR,
    resolve_paper_path,
)

# Session registry, keyed by the id we stash in each browser's gr.State.
SESSIONS: Dict[str, PipelineSession] = {}

POLL_SECONDS = 1.5

# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------

STEPS = [
    ("setup", "Setup"),
    ("outline", "Outline"),
    ("slides", "Slides"),
    ("done", "Done"),
]

# Map a pipeline state → the stepper key that should be highlighted.
_STATE_TO_STEP = {
    "setup": "setup",
    "outline_feedback": "outline",
    "generation_feedback": "slides",
    "processing": None,      # transient — keep the previous step lit
    "done": "done",
    "error": None,
}

# Papers offered in the dropdown are simply every PDF in the study dataset
# folder.
DATASET_DIR = ROOT / "dataset"

_PAPER_STEM_RE = re.compile(r"^paper(\d+)_(.+?)_([A-Za-z]+)_(\d{4})$")


def _paper_label(pdf_name: str) -> Tuple[int, str]:
    """(sort key, human label) for a dataset PDF filename."""
    stem = unquote(Path(pdf_name).stem)
    m = _PAPER_STEM_RE.match(stem)
    if not m:
        return (10**6, stem.replace("_", " "))
    num, title, venue, year = m.groups()
    title = title.replace("_", " ").replace(" - ", " — ").strip()
    return (int(num), f"paper{num} · {title} ({venue} {year})")


def dataset_paper_choices() -> List[Tuple[str, str]]:
    """(label, filename) dropdown choices for every PDF in dataset/."""
    entries = []
    for p in sorted(DATASET_DIR.glob("*.pdf")):
        key, label = _paper_label(p.name)
        entries.append((key, label, p.name))
    return [(label, name) for _key, label, name in sorted(entries)]


def stepper_html(active_key: Optional[str]) -> str:
    order = [k for k, _ in STEPS]
    idx = order.index(active_key) if active_key in order else -1
    chips = []
    for i, (key, label) in enumerate(STEPS):
        if idx >= 0 and i < idx:
            cls, dot = "step done", "✓"
        elif idx >= 0 and i == idx:
            cls, dot = "step active", str(i + 1)
        else:
            cls, dot = "step", str(i + 1)
        chips.append(
            f'<div class="{cls}"><span class="dot">{dot}</span>{label}</div>'
        )
    return f'<div class="steps">{"".join(chips)}</div>'


def outline_markdown(slides) -> str:
    if not slides:
        return "_Waiting for the outline…_"
    parts = []
    for o in slides:
        n = html.escape(str(o.get("n", "?")))
        title = html.escape(o.get("title") or "(untitled)")
        idea = (o.get("idea") or "").strip()
        card = [f"<div class='oslide'>"
                f"<div class='ohead'><span class='onum'>{n}</span>{title}</div>"]
        if idea:
            card.append(f"<div class='oidea'><span class='olbl'>💡 Idea</span> "
                        f"{html.escape(idea)}</div>")
        card.append("</div>")
        parts.append("".join(card))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def start_session(model, paper, file_obj, duration, audience,
                  run_name, instructions):
    """Create + launch a PipelineSession; returns (sid, error_message)."""
    if model not in MODELS:
        return None, f"Unknown model: {model}"

    sid = uuid.uuid4().hex[:12]
    workdir = SESSIONS_DIR / sid
    workdir.mkdir(parents=True, exist_ok=True)

    # Uploaded file wins over a chosen provided paper.
    if file_obj:
        src = file_obj if isinstance(file_obj, str) else getattr(file_obj, "name", None)
        if not src or not Path(src).is_file():
            return None, "Could not read the uploaded PDF."
        base_stem = Path(src).stem.replace(" ", "_")
        pdf_path = str(workdir / "input.pdf")
        shutil.copyfile(src, pdf_path)
    elif paper:
        resolved = resolve_paper_path(paper)
        if not resolved:
            return None, f"Paper not found: {paper}"
        pdf_path = resolved
        base_stem = Path(resolved).stem.replace(" ", "_")
    else:
        return None, "Upload a PDF or choose a provided paper first."

    paper_name = f"{base_stem}__{sid}"
    settings = {
        "model_label": model,
        "model_key": MODELS[model],
        "duration": int(duration),
        "audience": (audience or "researchers").strip() or "researchers",
        "instructions": (instructions or "").strip(),
        "run_name": (run_name or "").strip(),
        "base_stem": base_stem,
    }
    session = PipelineSession(sid, settings, pdf_path, paper_name)
    SESSIONS[sid] = session
    session.start()
    return sid, None


def _get(sid) -> Optional[PipelineSession]:
    return SESSIONS.get(sid) if sid else None


# ---------------------------------------------------------------------------
# UI construction
# ---------------------------------------------------------------------------

CSS = """
:root, .gradio-container { --arc-brand:#7c5cff; --arc-brand2:#00d4b1; }
.gradio-container { max-width: 1100px !important; margin: 0 auto !important; }

#arc-header {
  display:flex; align-items:center; gap:16px; padding:22px 26px; margin-bottom:6px;
  border-radius:18px; color:#fff;
  background:linear-gradient(120deg,#5b3df0 0%, #7c5cff 45%, #00b89b 110%);
  box-shadow:0 12px 40px rgba(92,60,255,.28);
}
#arc-header .logo{
  width:48px;height:48px;border-radius:14px;display:grid;place-items:center;
  font-weight:800;font-size:24px;color:#3a1d9e;background:#fff;flex:0 0 auto;
  box-shadow:0 4px 14px rgba(0,0,0,.18);
}
#arc-header h1{margin:0;font-size:22px;font-weight:750;letter-spacing:.2px;}
#arc-header .sub{opacity:.92;font-size:13.5px;margin-top:2px;}

.steps{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 6px;}
.step{display:flex;align-items:center;gap:8px;padding:6px 13px;border-radius:999px;
  font-size:12.5px;font-weight:600;border:1px solid var(--border-color-primary);
  color:var(--body-text-color-subdued);background:var(--background-fill-secondary);}
.step .dot{width:19px;height:19px;border-radius:50%;display:grid;place-items:center;
  font-size:11px;background:var(--background-fill-primary);
  border:1px solid var(--border-color-primary);color:inherit;}
.step.active{border-color:var(--arc-brand);color:var(--body-text-color);
  box-shadow:0 0 0 1px var(--arc-brand) inset;}
.step.active .dot{background:var(--arc-brand);color:#fff;border-color:var(--arc-brand);}
.step.done{color:var(--body-text-color);}
.step.done .dot{background:var(--arc-brand2);color:#062018;border-color:var(--arc-brand2);}

.oslide{border:1px solid var(--border-color-primary);border-radius:12px;
  padding:11px 14px;margin-bottom:9px;background:var(--background-fill-secondary);}
.oslide .ohead{font-weight:700;font-size:14.5px;display:flex;gap:8px;align-items:baseline;
  color:var(--body-text-color);}
.oslide .onum{display:inline-grid;place-items:center;min-width:20px;height:20px;padding:0 5px;
  border-radius:6px;background:var(--arc-brand);color:#fff;font-size:12px;font-weight:700;
  flex:0 0 auto;line-height:1;}
.oslide .oidea{margin-top:6px;font-size:13px;color:var(--body-text-color-subdued);}
.oslide .oidea .olbl{font-weight:650;color:var(--arc-brand);}

.arc-proc{display:flex;flex-direction:column;align-items:center;gap:16px;padding:34px 0;}
.arc-spinner{width:52px;height:52px;border-radius:50%;
  border:5px solid var(--background-fill-secondary);border-top-color:var(--arc-brand);
  animation:arc-spin 1s linear infinite;}
@keyframes arc-spin{to{transform:rotate(360deg)}}
.arc-proc .msg{font-size:16px;font-weight:650;}
.arc-proc .sub{color:var(--body-text-color-subdued);font-size:13px;max-width:520px;text-align:center;}

#arc-done-banner{font-size:22px;font-weight:750;text-align:center;margin:6px 0;}
.arc-badge{display:inline-block;padding:3px 11px;border-radius:999px;font-size:12px;
  font-weight:650;background:rgba(0,212,177,.16);color:#0aa588;}
"""


def build_demo():
    theme = gr.themes.Soft(
        primary_hue=gr.themes.colors.violet,
        secondary_hue=gr.themes.colors.teal,
        font=[gr.themes.GoogleFont("Inter"), "system-ui", "sans-serif"],
    )

    with gr.Blocks(title="ConvDeck: Presentation Study") as demo:
        demo._arc_theme = theme      # stashed so __main__ can pass them to launch()
        demo._arc_css = CSS
        sid_state = gr.State(None)
        key_state = gr.State("setup")       # last rendered stage key (anti-flicker)

        gr.HTML(
            '<div id="arc-header">'
            '<div class="logo">A</div>'
            '<div><h1>ConvDeck · Presentation Study</h1>'
            '<div class="sub">Turn a paper into slides, and steer the result with your feedback.</div>'
            '</div></div>'
        )
        stepper = gr.HTML(stepper_html("setup"))

        # ── 1. SETUP ────────────────────────────────────────────────────────
        with gr.Column(visible=True) as setup_col:
            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("### 1 · Choose your paper")
                    paper_dd = gr.Dropdown(
                        choices=dataset_paper_choices(), value=None,
                        label="Pick a study paper (dataset/)",
                        info="Every PDF in the dataset/ folder. Pick one, or "
                             "upload your own below.")
                    file_in = gr.File(
                        label="…or upload your own research PDF "
                              "(overrides the dropdown)",
                        file_types=[".pdf"], type="filepath")
                    gr.Markdown("### 2 · Presentation settings")
                    with gr.Row():
                        model_dd = gr.Dropdown(list(MODELS.keys()), value=DEFAULT_MODEL,
                                               label="Model")
                        audience_dd = gr.Dropdown(AUDIENCE_PRESETS, value=AUDIENCE_PRESETS[0],
                                                  label="Target audience",
                                                  allow_custom_value=True)
                    with gr.Row():
                        duration_sl = gr.Slider(5, 45, value=20, step=5,
                                                label="Duration (minutes)")
                        run_name_in = gr.Textbox(
                            label="Run name",
                            placeholder="e.g. my-run-01 (optional)")
                    instr_in = gr.Textbox(
                        label="High-level instructions", lines=3,
                        placeholder="e.g. Emphasize the experimental results and keep it "
                                    "high-level for a broad ML audience. (optional)")
            start_btn = gr.Button("Generate my slides  →", variant="primary", size="lg")
            setup_err = gr.Markdown(visible=False)

        # ── 2. PROCESSING ───────────────────────────────────────────────────
        with gr.Column(visible=False) as proc_col:
            proc_html = gr.HTML(_proc_html("Working…"))

        # ── 3. OUTLINE REVIEW ───────────────────────────────────────────────
        with gr.Column(visible=False) as outline_col:
            outline_head = gr.Markdown("## Review the outline")
            outline_md = gr.Markdown()
            outline_fb = gr.Textbox(
                label="Your feedback", lines=3, interactive=True,
                placeholder="What should change? e.g. 'Add a slide on limitations', "
                            "'Merge slides 2 and 3'.")
            with gr.Row():
                outline_send = gr.Button("Send feedback", variant="secondary")
                outline_ok = gr.Button("OK, approve outline  ✓", variant="primary")

        # ── 4. SLIDES REVIEW ────────────────────────────────────────────────
        with gr.Column(visible=False) as slides_col:
            slides_head = gr.Markdown("## Review your slides")
            slides_gallery = gr.Gallery(label="Slide deck", columns=2, height=460,
                                        object_fit="contain", show_label=False)
            slides_fb = gr.Textbox(
                label="Your feedback", lines=3, interactive=True,
                placeholder="Refine the deck: e.g. 'Shorten the bullets on slide 4', "
                            "'Add a takeaways slide'.")
            with gr.Row():
                slides_send = gr.Button("Send feedback", variant="secondary")
                slides_ok = gr.Button("OK, approve slides  ✓", variant="primary")

        # ── 5. DONE ─────────────────────────────────────────────────────────
        with gr.Column(visible=False) as done_col:
            gr.HTML('<div id="arc-done-banner">🎉 Your presentation is ready</div>')
            done_summary = gr.Markdown()
            done_file = gr.DownloadButton("⬇  Download .pptx", variant="primary")
            done_gallery = gr.Gallery(label="Final deck", columns=2, height=460,
                                      object_fit="contain", show_label=False)
            restart_btn = gr.Button("Start another", variant="secondary")

        # ── 6. ERROR ────────────────────────────────────────────────────────
        with gr.Column(visible=False) as error_col:
            gr.Markdown("## Something went wrong")
            error_md = gr.Markdown()
            restart_btn2 = gr.Button("Back to start", variant="secondary")

        # ── Shared: live pipeline log (collapsible) ─────────────────────────
        with gr.Accordion("Show pipeline log", open=False):
            log_box = gr.Code(label="", language=None, lines=14, interactive=False)

        timer = gr.Timer(POLL_SECONDS, active=False)

        # ── All stage columns, in the order used by the tick's update dict ──
        stage_cols = [setup_col, proc_col, outline_col,
                      slides_col, done_col, error_col]

        # ================================================================
        # Event wiring
        # ================================================================

        def on_start(model, paper, file_obj, duration, audience, run_name, instr):
            sid, err = start_session(model, paper, file_obj, duration, audience,
                                     run_name, instr)
            if err:
                return (None, "setup",
                        gr.update(visible=True, value=f"⚠️ {err}"),
                        gr.update(active=False),
                        *[gr.update() for _ in stage_cols])
            # Switch to processing, arm the timer.
            col_updates = [gr.update(visible=(c is proc_col)) for c in stage_cols]
            return (sid, "processing",
                    gr.update(visible=False),
                    gr.update(active=True),
                    *col_updates)

        start_btn.click(
            on_start,
            [model_dd, paper_dd, file_in, duration_sl, audience_dd,
             run_name_in, instr_in],
            [sid_state, key_state, setup_err, timer, *stage_cols],
        )

        # ---- polling tick -------------------------------------------------
        def on_tick(sid, last_key):
            session = _get(sid)
            if session is None:
                return {}
            s = session.status()
            state = s["state"]
            key = _render_key(s)
            step_key = _STATE_TO_STEP.get(state)

            out = {log_box: gr.update(value=s.get("log_tail", ""))}

            # Always re-assert which stage column must be visible.  Gradio can
            # occasionally lose a component visibility update while timer events
            # overlap; returning early before these updates can leave every stage
            # column hidden, with only the shared pipeline log visible.
            target = {
                "outline_feedback": outline_col,
                "generation_feedback": slides_col,
                "processing": proc_col, "done": done_col, "error": error_col,
            }.get(state, proc_col)
            for c in stage_cols:
                out[c] = gr.update(visible=(c is target))

            if step_key is not None:
                out[stepper] = stepper_html(step_key)

            if key == last_key:
                # Do not rebuild stage contents on every poll (that would erase
                # text while the participant is typing), but keep visibility
                # synchronized on every tick.
                return out

            out[key_state] = key

            if state == "outline_feedback":
                rnd = s.get("round", 0) + 1
                n = len(s.get("outline") or [])
                out[outline_head] = (f"## Review the outline &nbsp;"
                                     f"<span class='arc-badge'>awaiting your input</span>\n"
                                     f"Round {rnd} · {n} slides.")
                out[outline_md] = outline_markdown(s.get("outline"))
                out[outline_fb] = gr.update(value="")
                out[outline_send] = gr.update(interactive=True)
                out[outline_ok] = gr.update(interactive=True)
            elif state == "generation_feedback":
                rnd = s.get("round", 0) + 1
                n = s.get("num_slides", 0)
                q = s.get("js_question")
                if q:
                    # The JS feedback agent is asking a clarifying question —
                    # surface it so the participant answers it in the box below.
                    out[slides_head] = (
                        f"## The assistant has a question &nbsp;"
                        f"<span class='arc-badge'>awaiting your answer</span>\n"
                        f"> {html.escape(q)}")
                else:
                    out[slides_head] = (f"## Review your slides &nbsp;"
                                        f"<span class='arc-badge'>awaiting your input</span>\n"
                                        f"Round {rnd} · {n} slides.")
                out[slides_gallery] = [str(p) for p in session.gen_slides]
                out[slides_fb] = gr.update(value="")
                out[slides_send] = gr.update(interactive=True)
                out[slides_ok] = gr.update(interactive=True)
            elif state == "processing":
                out[proc_html] = _proc_html(s.get("phase", "Working…"))
            elif state == "done":
                out[done_summary] = f"**{s.get('num_slides', 0)} slides**"
                out[done_gallery] = [str(p) for p in session.final_slides]
                out[done_file] = gr.update(value=session.final_pptx)
                out[timer] = gr.update(active=False)
            elif state == "error":
                out[error_md] = s.get("message", "The pipeline exited unexpectedly.")
                out[timer] = gr.update(active=False)

            return out

        tick_outputs = [
            log_box, key_state, stepper, timer,
            *stage_cols,
            outline_head, outline_md, outline_fb,
            outline_send, outline_ok,
            slides_head, slides_gallery, slides_fb,
            slides_send, slides_ok,
            proc_html,
            done_summary, done_gallery, done_file,
            error_md,
        ]
        # show_progress="hidden": the default per-event loading overlay would
        # cover every output component (incl. the feedback textboxes) on each
        # 1.5 s tick, making them impossible to type into.
        timer.tick(on_tick, [sid_state, key_state], tick_outputs,
                   show_progress="hidden")

        # ---- interaction handlers ----------------------------------------
        def do_outline_fb(sid, text):
            if not (text or "").strip():
                # An empty send would approve (backend sends "ok") — make the
                # participant use the explicit OK button for that.
                gr.Warning("Type your feedback first, or click OK to approve.")
                return gr.update(), gr.update()
            s = _get(sid)
            if s:
                s.send_feedback(text)
            return gr.update(interactive=False), gr.update(interactive=False)

        outline_send.click(do_outline_fb, [sid_state, outline_fb],
                           [outline_send, outline_ok])

        def do_outline_ok(sid):
            s = _get(sid)
            if s:
                s.approve()
            return gr.update(interactive=False), gr.update(interactive=False)

        outline_ok.click(do_outline_ok, [sid_state], [outline_send, outline_ok])

        def do_slides_fb(sid, text):
            if not (text or "").strip():
                gr.Warning("Type your feedback first, or click OK to approve.")
                return gr.update(), gr.update()
            s = _get(sid)
            if s:
                s.send_feedback(text)
            return gr.update(interactive=False), gr.update(interactive=False)

        slides_send.click(do_slides_fb, [sid_state, slides_fb],
                          [slides_send, slides_ok])

        def do_slides_ok(sid):
            s = _get(sid)
            if s:
                s.approve()
            return gr.update(interactive=False), gr.update(interactive=False)

        slides_ok.click(do_slides_ok, [sid_state], [slides_send, slides_ok])

        # ---- restart ------------------------------------------------------
        def do_restart():
            col_updates = [gr.update(visible=(c is setup_col)) for c in stage_cols]
            return (None, "setup", stepper_html("setup"),
                    gr.update(active=False), gr.update(visible=False, value=""),
                    *col_updates)

        restart_outputs = [sid_state, key_state, stepper, timer, setup_err, *stage_cols]
        restart_btn.click(do_restart, None, restart_outputs)
        restart_btn2.click(do_restart, None, restart_outputs)

    return demo


def _proc_html(msg: str) -> str:
    return (
        '<div class="arc-proc"><div class="arc-spinner"></div>'
        f'<div class="msg">{msg}</div>'
        '<div class="sub">This step runs language &amp; vision models. It can take a '
        'couple of minutes. You can watch the log below.</div></div>'
    )


def _render_key(s: dict) -> str:
    """Stage identity used to avoid rebuilding the UI on every poll tick."""
    st = s["state"]
    if st == "outline_feedback":
        return f"of:{s.get('round', 0)}"
    if st == "generation_feedback":
        return f"gf:{s.get('round', 0)}:{s.get('slides_token')}:{s.get('num_slides')}"
    if st == "processing":
        return f"pr:{s.get('phase', '')}"
    if st == "done":
        return f"done:{s.get('num_slides', 0)}"
    if st == "error":
        return "err"
    return st


def _find_free_port(host, start_port, tries=100):
    """Return the first bindable port at or above ``start_port``.

    Lets several instances launch at once: each grabs the next free port
    instead of crashing on 'address already in use'. There is an inherent
    probe→launch race between processes, so the launch below still retries.
    """
    import socket

    for candidate in range(start_port, start_port + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(
        f"[gradio_app] No free port in {start_port}..{start_port + tries - 1}")


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Launch the ConvDeck study UI.")
    parser.add_argument("--host", default=os.environ.get("CONVDECK_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("CONVDECK_PORT", "7860")),
                        help="Preferred port; the next free one is used if taken.")
    parser.add_argument("--no-share", dest="share", action="store_false",
                        help="Disable the public *.gradio.live tunnel link.")
    parser.set_defaults(
        share=os.environ.get("CONVDECK_SHARE", "1") not in ("0", "false", "False", ""))
    cli = parser.parse_args()

    host, share = cli.host, cli.share
    # Probe from the requested port so multiple instances never collide.
    port = _find_free_port("127.0.0.1" if host == "0.0.0.0" else host, cli.port)
    if port != cli.port:
        print(f"[gradio_app] port {cli.port} busy → using {port}")

    print(f"[gradio_app] dataset papers found: {len(dataset_paper_choices())}")
    print(f"[gradio_app] public share link: {'enabled, see the URL below' if share else 'disabled'}")
    demo = build_demo()

    # Retry on the (rare) race where another instance grabbed the probed port
    # between the probe above and Gradio's own bind.
    last_err = None
    for _ in range(50):
        print(f"[gradio_app] ConvDeck study UI → http://{host}:{port}")
        try:
            demo.queue(default_concurrency_limit=None).launch(
                server_name=host, server_port=port, share=share,
                show_error=True, theme=demo._arc_theme, css=demo._arc_css)
            break
        except OSError as e:
            last_err = e
            port = _find_free_port(
                "127.0.0.1" if host == "0.0.0.0" else host, port + 1)
    else:
        raise SystemExit(f"[gradio_app] Could not bind a port: {last_err}")