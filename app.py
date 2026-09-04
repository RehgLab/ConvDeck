#!/usr/bin/env python3
"""
ConvDeck — User-Study Web UI (FastAPI)
=====================================

A polished, guided web interface for running the *real-user* slide-generation
study.  It drives the interactive pipeline

    python -m slide_generation.pipeline <pdf> --interactive

(i.e. the human path — with ``--interactive`` the pipeline blocks on ``input()``
at each interaction point; omitting it runs the default LLM/VLM simulator)
and walks a participant through the two interaction points the pipeline pauses
at:

    1. Outline review     → free-form feedback / approve   (loops)
    2. Slide-deck review  → free-form feedback / approve   (loops, live PNGs)

The pipeline is a terminal program that blocks on ``input()``.  This app spawns
it as a subprocess, reads its stdout, recognises the prompt banners it prints,
and exposes the current state over a small JSON API that the single-page UI
polls.  Feedback / approvals are written back to the subprocess stdin.

Each participant session gets a unique ``paper_name`` so concurrent runs never
collide on ``contents/<name>/`` or ``tmp/<name>/``.  When a shared Docling cache
exists for the base paper, it is symlinked into the session name so the
expensive PDF conversion is not repeated.

Run (from the repo root, with deps installed):
    python app.py            # http://<host>:7860
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

# ── Make LibreOffice discoverable (pptx → png) for us and the subprocess ─────
_lo_programs = glob.glob(os.path.expanduser("~/libreoffice/opt/libreoffice*/program"))
if _lo_programs:
    os.environ["PATH"] = _lo_programs[-1] + os.pathsep + os.environ.get("PATH", "")
SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")

from dotenv import load_dotenv

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

load_dotenv()

# ---------------------------------------------------------------------------
# Paths & static config
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
CONTENTS = ROOT / "contents"
TMP = ROOT / "tmp"
DOCLING_CACHE = ROOT / "docling_cache"
SESSIONS_DIR = ROOT / "study_sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Display label → pipeline --model key.
MODELS: Dict[str, str] = {
    "GPT-5": "gpt-5",
    "GPT-4o": "4o",
}
DEFAULT_MODEL = "GPT-5"

AUDIENCE_PRESETS = [
    "researchers",
    "graduate students",
    "undergraduate students",
    "general audience",
    "industry practitioners",
]

# Directories scanned for ready-to-use study PDFs.
PAPER_DIRS = [ROOT / "study_papers", ROOT / "dataset", ROOT]


def _pipeline_python() -> str:
    return sys.executable


def _subprocess_env() -> Dict[str, str]:
    """Environment for the pipeline subprocess with single-key fallbacks filled."""
    env = os.environ.copy()
    env.setdefault("LC_ALL", "en_US.UTF-8")
    env.setdefault("LANG", "en_US.UTF-8")
    # The interactive agents read the *unnumbered* key names; fall back to the
    # first numbered key from the parallel-batch .env when a plain one is absent.
    for base in ("OPENAI_API_KEY", "GEMINI_API_KEY", "PAPERCLIP_MCP_API_KEY"):
        if not env.get(base) and env.get(f"{base}_1"):
            env[base] = env[f"{base}_1"]
    if not env.get("GOOGLE_API_KEY") and env.get("GEMINI_API_KEY"):
        env["GOOGLE_API_KEY"] = env["GEMINI_API_KEY"]
    return env


def list_available_papers() -> List[str]:
    seen: Dict[str, str] = {}
    for d in PAPER_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.pdf")):
            if p.name not in seen:
                seen[p.name] = str(p)
    return list(seen.keys())


def resolve_paper_path(name: str) -> Optional[str]:
    for d in PAPER_DIRS:
        cand = d / name
        if cand.is_file():
            return str(cand)
    return None


# ---------------------------------------------------------------------------
# pptx → png rendering helper (final deck + safety fallbacks)
# ---------------------------------------------------------------------------

def pptx_to_pngs(pptx_path: str, out_dir: Path, dpi: int = 110) -> List[Path]:
    """Convert a .pptx to per-slide PNGs via LibreOffice + pdf2image."""
    import tempfile
    from pdf2image import convert_from_path

    out_dir.mkdir(parents=True, exist_ok=True)
    if not SOFFICE:
        return []
    with tempfile.TemporaryDirectory() as pdf_dir, tempfile.TemporaryDirectory() as user_install:
        try:
            subprocess.run(
                [SOFFICE, "--headless", "--norestore", "--nolockcheck",
                 f"-env:UserInstallation=file://{user_install}",
                 "--convert-to", "pdf", pptx_path, "--outdir", pdf_dir],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=240, env=_subprocess_env(),
            )
        except Exception:
            return []
        pdfs = [f for f in os.listdir(pdf_dir) if f.endswith(".pdf")]
        if not pdfs:
            return []
        try:
            images = convert_from_path(os.path.join(pdf_dir, pdfs[0]), dpi=dpi)
        except Exception:
            return []
        paths = []
        for i, img in enumerate(images, 1):
            out = out_dir / f"slide_{i:04d}.png"
            img.save(out, "PNG")
            paths.append(out)
        return paths


# ---------------------------------------------------------------------------
# Session: one running pipeline + its interaction state machine
# ---------------------------------------------------------------------------

# Prompt banners the pipeline prints right before it blocks on input().
_RE_OUTLINE_REVIEW = re.compile(r"^RAW CONTENT RST REVIEW \(round (\d+)\).*$", re.M)
# The interactive deck-feedback loop emits a "JS DECK REVIEW (round N)" banner
# each round, and the agent may interject clarifying questions. Both are pending
# interactions routed through the same deck-feedback screen (previews + feedback box).
_RE_JS_REVIEW = re.compile(r"^JS DECK REVIEW \(round (\d+)\)", re.M)
_RE_JS_ASK = re.compile(r"^\[js feedback SPEAK\] Agent asks: (.*)$", re.M)
_SEP_RE = re.compile(r"^=+\s*$")


class PipelineSession:
    def __init__(self, sid: str, settings: dict, pdf_path: str, paper_name: str):
        self.sid = sid
        self.settings = settings
        self.pdf_path = pdf_path
        self.paper_name = paper_name
        self.model_key = settings["model_key"]
        self.workdir = SESSIONS_DIR / sid
        self.workdir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._final_lock = threading.Lock()
        self._log: List[str] = []
        self.last_output_ts = time.time()
        self.proc: Optional[subprocess.Popen] = None
        self.returncode: Optional[int] = None
        self.error: Optional[str] = None

        # answered-counters (what the participant has already responded to)
        self.outline_fb_answered = 0
        self.gen_fb_answered = 0

        # study record
        self.started_at = time.time()
        self.events: List[dict] = []

        # cached generation preview set (round token + png paths)
        self.gen_round_token = -1
        self.gen_slides: List[Path] = []

        # final render
        self.final_pptx: Optional[str] = None
        self.final_slides: List[Path] = []
        self._finalized = False

        self.base_stem: Optional[str] = settings.get("base_stem")

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        s = self.settings
        cmd = [
            _pipeline_python(), "-u", "-m", "slide_generation.pipeline",
            self.pdf_path,
            "--paper_name", self.paper_name,
            "--model", self.model_key,
            "--duration", str(s["duration"]),
            "--audience", s["audience"],
            "--user_instructions", s["instructions"],
            # Interactive human-study mode: the pipeline blocks on input() for the
            # participant's outline/deck feedback. The conversational stages and
            # interaction logging are on by default, so no extra flags.
            "--interactive",
        ]
        self._seed_docling_cache()
        (self.workdir / "command.txt").write_text(" ".join(cmd), encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=_subprocess_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def _seed_docling_cache(self):
        """Symlink a shared Docling cache into this session's paper_name."""
        base = self.settings.get("base_stem")
        if not base:
            return
        src = DOCLING_CACHE / base
        dst = DOCLING_CACHE / self.paper_name
        if src.is_dir() and not dst.exists():
            try:
                dst.symlink_to(src.resolve(), target_is_directory=True)
            except OSError:
                pass

    def _reader(self):
        assert self.proc and self.proc.stdout
        for line in iter(self.proc.stdout.readline, ""):
            with self._lock:
                self._log.append(line)
                self.last_output_ts = time.time()
        self.proc.wait()
        self.returncode = self.proc.returncode

    def _joined_log(self) -> str:
        with self._lock:
            return "".join(self._log)

    def log_tail(self, n_chars: int = 4000) -> str:
        return self._joined_log()[-n_chars:]

    # ── stdin writers ────────────────────────────────────────────────────
    def _send(self, text: str) -> bool:
        if not self.proc or self.proc.stdin is None or self.proc.poll() is not None:
            return False
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def send_feedback(self, text: str):
        text = (text or "").strip()
        state = self.compute_state()["state"]
        if state == "outline_feedback":
            self._send(text or "ok")
            self.outline_fb_answered += 1
            self.events.append({"t": self._elapsed(), "stage": "outline_feedback",
                                "type": "revise", "text": text})
        elif state == "generation_feedback":
            self._send(text or "ok")
            self.gen_fb_answered += 1
            self.events.append({"t": self._elapsed(), "stage": "generation_feedback",
                                "type": "revise", "text": text})

    def approve(self):
        state = self.compute_state()["state"]
        if state == "outline_feedback":
            self._send("ok")
            self.outline_fb_answered += 1
            self.events.append({"t": self._elapsed(), "stage": "outline_feedback",
                                "type": "approve"})
        elif state == "generation_feedback":
            self._send("ok")
            self.gen_fb_answered += 1
            self.events.append({"t": self._elapsed(), "stage": "generation_feedback",
                                "type": "approve"})

    def _elapsed(self) -> float:
        return round(time.time() - self.started_at, 1)

    # ── state machine ────────────────────────────────────────────────────
    def _counts(self, log: str) -> dict:
        return {
            "outline_fb": len(_RE_OUTLINE_REVIEW.findall(log)),
            "gen_fb": len(_RE_JS_REVIEW.findall(log))
                      + len(_RE_JS_ASK.findall(log)),
        }

    def compute_state(self) -> dict:
        log = self._joined_log()
        c = self._counts(log)
        alive = self.proc is not None and self.proc.poll() is None

        # terminal states
        if not alive and self.returncode is not None:
            if self.returncode != 0:
                return {"state": "error", "counts": c}
            return {"state": "done", "counts": c}

        # first pending interaction in pipeline order
        if c["outline_fb"] > self.outline_fb_answered:
            return {"state": "outline_feedback", "counts": c,
                    "round": self.outline_fb_answered}
        if c["gen_fb"] > self.gen_fb_answered:
            return {"state": "generation_feedback", "counts": c,
                    "round": self.gen_fb_answered}

        # nothing pending → the pipeline is computing something
        return {"state": "processing", "counts": c}

    def js_pending_question(self) -> Optional[str]:
        """If the JS feedback agent's latest prompt is a clarifying question
        (rather than a normal round review), return the question text.

        The agent may interject ``[js feedback SPEAK] Agent asks: …`` between
        review rounds; we surface that so the participant answers the right
        thing in the shared slides-feedback box.
        """
        log = self._joined_log()
        asks = list(_RE_JS_ASK.finditer(log))
        if not asks:
            return None
        revs = list(_RE_JS_REVIEW.finditer(log))
        last_rev_end = revs[-1].end() if revs else -1
        last_ask = asks[-1]
        if last_ask.start() > last_rev_end:
            return last_ask.group(1).strip()
        return None

    def phase_hint(self, c: dict) -> str:
        if c["outline_fb"] == 0:
            return "Analyzing the paper and building the presentation outline…"
        if c["gen_fb"] == 0:
            return "Composing the slide deck from your approved outline…"
        return "Applying your changes and finalizing your presentation…"

    # ── previews ─────────────────────────────────────────────────────────
    def outline_slides(self) -> List[dict]:
        """Parse the latest 'RAW CONTENT RST REVIEW' block into slide cards."""
        log = self._joined_log()
        matches = list(_RE_OUTLINE_REVIEW.finditer(log))
        if not matches:
            return []
        start = matches[-1].end()
        lines = log[start:].splitlines()
        # skip the header line's trailing fragment, blank lines, and the
        # separator row(s) that box the banner
        i = 0
        while i < len(lines) and (lines[i].strip() == "" or _SEP_RE.match(lines[i])):
            i += 1
        body: List[str] = []
        for ln in lines[i:]:
            if _SEP_RE.match(ln):
                break
            body.append(ln)
        slides: List[dict] = []
        cur: Optional[dict] = None
        for ln in body:
            m = re.match(r"^\[(\d+)\]\s*(.*)$", ln)
            if m:
                if cur:
                    slides.append(cur)
                cur = {"n": int(m.group(1)), "title": m.group(2).strip(), "idea": ""}
            elif cur is not None:
                txt = ln.strip()
                if txt.lower().startswith("idea:"):
                    txt = txt[5:].strip()
                cur["idea"] = (cur["idea"] + " " + txt).strip() if cur["idea"] else txt
        if cur:
            slides.append(cur)
        return slides

    def refresh_gen_slides(self):
        """Point gen_slides at the newest feedback round's rendered slides.

        Prefers the JS feedback loop's snapshots
        (tmp/<paper>/js_feedback/feedback{n}/slides/slide_*.jpg), falling back
        to the legacy generation-feedback location
        (tmp/<paper>/feedback{n}/slides/slide_*.png).
        """
        for base in (TMP / self.paper_name / "js_feedback", TMP / self.paper_name):
            if not base.is_dir():
                continue
            best_idx, best_slides = -1, []
            for d in base.glob("feedback*"):
                m = re.match(r"feedback(\d+)$", d.name)
                if not m:
                    continue
                slides = (sorted((d / "slides").glob("slide_*.jpg"))
                          or sorted((d / "slides").glob("slide_*.png")))
                if slides and int(m.group(1)) >= best_idx:
                    best_idx, best_slides = int(m.group(1)), slides
            if best_idx >= 0:
                self.gen_round_token = best_idx
                self.gen_slides = best_slides
                return

    def finalize(self):
        """Render the final deck to PNGs and save the study record (once)."""
        with self._final_lock:
            if self._finalized:
                return
            self._finalized = True
        # Prefer the JS/PptxGenJS-rendered deck (the one the participant edited
        # via --js_feedback). Its filename is *_output_slides_pptxgenjs_*.pptx,
        # which deliberately does NOT match the plain "*_output_slides.pptx"
        # glob used for the non-JS fallback.
        cdir = CONTENTS / self.paper_name
        js = sorted(cdir.glob("*_output_slides_pptxgenjs_*.pptx"))
        if js:
            pptx = js[-1]
        else:
            pptx = cdir / f"{self.model_key}_{self.model_key}_output_slides.pptx"
            if not pptx.is_file():
                cands = sorted(cdir.glob("*_output_slides.pptx"))
                pptx = cands[0] if cands else None
        if pptx and pptx.is_file():
            self.final_pptx = str(pptx)
            self.final_slides = pptx_to_pngs(str(pptx), self.workdir / "final_slides")
        self._save_record()

    def _save_record(self):
        prefix = f"<{self.model_key}_{self.model_key}>"
        rec = {
            "session_id": self.sid,
            "run_name": self.settings.get("run_name", ""),
            "paper_name": self.paper_name,
            "pdf": os.path.basename(self.pdf_path),
            "settings": {k: self.settings[k] for k in
                         ("model_label", "duration", "audience", "instructions")},
            "started_at": self.started_at,
            "finished_at": time.time(),
            "duration_sec": round(time.time() - self.started_at, 1),
            "events": self.events,
            "output_pptx": self.final_pptx,
            "interaction_log": str(CONTENTS / self.paper_name / f"{prefix}_interaction_log.jsonl"),
            "returncode": self.returncode,
        }
        (self.workdir / "result.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")

    # ── status payload for the UI ────────────────────────────────────────
    def status(self) -> dict:
        st = self.compute_state()
        state, c = st["state"], st["counts"]
        payload: dict = {
            "session_id": self.sid,
            "state": state,
            "paper_name": self.paper_name,
            "log_tail": self.log_tail(),
        }
        if state == "outline_feedback":
            payload["round"] = st.get("round", 0)
            payload["outline"] = self.outline_slides()
        elif state == "generation_feedback":
            self.refresh_gen_slides()
            payload["round"] = st.get("round", 0)
            payload["num_slides"] = len(self.gen_slides)
            payload["slides_token"] = self.gen_round_token
            payload["js_question"] = self.js_pending_question()
        elif state == "processing":
            payload["phase"] = self.phase_hint(c)
        elif state == "done":
            self.finalize()
            payload["num_slides"] = len(self.final_slides)
            payload["download"] = f"/api/download/{self.sid}"
        elif state == "error":
            self._save_record()
            payload["message"] = "The pipeline stopped unexpectedly. See the log below."
        return payload


SESSIONS: Dict[str, PipelineSession] = {}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="ConvDeck — User Study")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/api/config")
def api_config():
    return {
        "models": list(MODELS.keys()),
        "default_model": DEFAULT_MODEL,
        "audiences": AUDIENCE_PRESETS,
        "papers": list_available_papers(),
        "duration": {"min": 5, "max": 45, "step": 5, "default": 20},
    }


@app.post("/api/start")
async def api_start(
    model: str = Form(...),
    duration: int = Form(...),
    audience: str = Form(...),
    instructions: str = Form(""),
    run_name: str = Form(""),
    paper: str = Form(""),
    file: Optional[UploadFile] = File(None),
):
    if model not in MODELS:
        raise HTTPException(400, f"Unknown model: {model}")

    sid = uuid.uuid4().hex[:12]
    workdir = SESSIONS_DIR / sid
    workdir.mkdir(parents=True, exist_ok=True)

    # Resolve the source PDF (uploaded file wins over a picked dataset paper).
    if file is not None and file.filename:
        base_stem = Path(file.filename).stem.replace(" ", "_")
        pdf_path = str(workdir / "input.pdf")
        with open(pdf_path, "wb") as fh:
            shutil.copyfileobj(file.file, fh)
    elif paper:
        resolved = resolve_paper_path(paper)
        if not resolved:
            raise HTTPException(400, f"Paper not found: {paper}")
        pdf_path = resolved
        base_stem = Path(resolved).stem.replace(" ", "_")
    else:
        raise HTTPException(400, "Upload a PDF or choose an available paper.")

    paper_name = f"{base_stem}__{sid}"
    settings = {
        "model_label": model,
        "model_key": MODELS[model],
        "duration": int(duration),
        "audience": audience.strip() or "researchers",
        "instructions": instructions.strip(),
        "run_name": run_name.strip(),
        "base_stem": base_stem,
    }
    session = PipelineSession(sid, settings, pdf_path, paper_name)
    SESSIONS[sid] = session
    session.start()
    return {"session_id": sid}


def _get(sid: str) -> PipelineSession:
    s = SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "Unknown session")
    return s


@app.get("/api/status/{sid}")
def api_status(sid: str):
    return _get(sid).status()


@app.post("/api/feedback/{sid}")
async def api_feedback(sid: str, text: str = Form("")):
    _get(sid).send_feedback(text)
    return {"ok": True}


@app.post("/api/approve/{sid}")
def api_approve(sid: str):
    _get(sid).approve()
    return {"ok": True}


@app.get("/api/slide/{sid}/{idx}")
def api_slide(sid: str, idx: int, v: int = 0):
    s = _get(sid)
    s.refresh_gen_slides()
    if 0 <= idx < len(s.gen_slides):
        path = s.gen_slides[idx]
        mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        return FileResponse(str(path), media_type=mime,
                            headers={"Cache-Control": "no-store"})
    raise HTTPException(404, "slide not found")


@app.get("/api/final-slide/{sid}/{idx}")
def api_final_slide(sid: str, idx: int):
    s = _get(sid)
    if 0 <= idx < len(s.final_slides):
        return FileResponse(str(s.final_slides[idx]), media_type="image/png")
    raise HTTPException(404, "slide not found")


@app.get("/api/download/{sid}")
def api_download(sid: str):
    s = _get(sid)
    if not s.final_pptx or not os.path.isfile(s.final_pptx):
        s.finalize()
    if s.final_pptx and os.path.isfile(s.final_pptx):
        fname = f"{s.settings.get('base_stem', 'ConvDeck')}_slides.pptx"
        return FileResponse(
            s.final_pptx, filename=fname,
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
    raise HTTPException(404, "Output not ready")


# The single-page UI lives in app_ui.py to keep this file focused on logic.
from app_ui import INDEX_HTML  # noqa: E402


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("CONVDECK_HOST", "0.0.0.0")
    port = int(os.environ.get("CONVDECK_PORT", "7860"))
    print(f"[app] ConvDeck study UI → http://{host}:{port}")
    print(f"[app] soffice: {SOFFICE or 'NOT FOUND (pptx previews will be unavailable)'}")
    print(f"[app] papers found: {len(list_available_papers())}")
    uvicorn.run(app, host=host, port=port, log_level="info")
