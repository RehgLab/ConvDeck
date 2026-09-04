"""
Paperclip retrieval backend for the reviser's ``arxiv_search`` tool.

Retrieves related papers and grounded passages for a query from the Paperclip
service (https://paperclip.gxl.ai) over its MCP HTTP transport
(``paperclip_http``), then optionally answers a per-paper extraction question
with an LLM. Exposes ``paper_retrieval_pipeline(query, ...)`` returning
``{papers, global_top_chunks, ...}``.

Requires a ``PAPERCLIP_MCP_API_KEY`` in the environment (get a key at
https://paperclip.gxl.ai). Retrieval failures are classified via the HTTP-layer
return codes from ``paperclip_http`` (``RC_AUTH``, ``RC_TIMEOUT``,
``RC_BACKEND``, ``RC_CLIENT``) and surfaced as human-readable advice — for
example, an auth failure points at ``PAPERCLIP_MCP_API_KEY``.

Run as a script for a quick connectivity / retrieval smoke test:
    python -m slide_generation.tools.paperclip_search --query "..."
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from slide_generation.tools import paperclip_http


# ── Config ───────────────────────────────────────────────────────────────────

SAVE_DIR = Path("./tmp/paperclip_search").resolve()
LOG_FILE = SAVE_DIR / "paperclip_search.log"

DEFAULT_TOP_K = 3
DEFAULT_QUERY = "Academic presentation generation with reference based slide generation"

DEFAULT_LLM_MODEL = "gpt-4o-mini"
# gpt-4o-mini has a 128k context. ~4 chars/token → 300k chars ≈ 75k tokens of
# prompt budget, leaves headroom for system prompt + completion.
DEFAULT_LLM_MAX_CHARS = 300_000


# ── Logger ───────────────────────────────────────────────────────────────────

logger = logging.getLogger("paperclip_search")
logger.setLevel(logging.INFO)
logger.propagate = False
# Module is silent by default. CLI use (`main()`) attaches file + console
# handlers; library use (importing helpers) leaves the NullHandler in place
# so log() calls are no-ops.
if not logger.handlers:
    logger.addHandler(logging.NullHandler())


def _setup_main_logging() -> None:
    """Attach file + console handlers for CLI use. Idempotent."""
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.info(f"Run @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Logging to: {LOG_FILE}")


def log(*args, **kwargs) -> None:
    sep = kwargs.get("sep", " ")
    logger.info(sep.join(str(a) for a in args))


# ── Output parsing ───────────────────────────────────────────────────────────
# The Paperclip MCP server returns the same text the ``paperclip`` CLI prints,
# so these parsers work against either transport.

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


_RECORD_KEY_ALIASES = {
    "title": ["title"],
    "id": ["id", "paper_id"],
    "source": ["source"],
    "abstract": ["abstract", "summary"],
    "url": ["url", "link"],
    "authors": ["authors", "author"],
    "date": ["date", "published", "publication_date"],
    "doi": ["doi"],
    "journal": ["journal"],
}


def _normalize_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map paperclip's JSON field names to our canonical keys.

    Defensive: paperclip may rename fields in future releases. Unknown keys
    are preserved under `_extra` so we can inspect them if results look off.
    """
    out: Dict[str, Any] = {}
    for canonical, aliases in _RECORD_KEY_ALIASES.items():
        for k in aliases:
            if k in raw and raw[k] not in (None, ""):
                v = raw[k]
                if canonical == "authors" and isinstance(v, list):
                    v = ", ".join(str(a) for a in v)
                out[canonical] = v
                break
    extras = {k: v for k, v in raw.items() if k not in {a for al in _RECORD_KEY_ALIASES.values() for a in al}}
    if extras:
        out["_extra"] = extras
    return out


def _parse_search_text(output: str) -> List[Dict[str, Any]]:
    """Parse `paperclip search` formatted text output (fallback for older CLIs
    that don't honor --json). Mirrors paperclip cli._parse_papers_from_output.
    """
    text = _strip_ansi(output)
    records: List[Dict[str, Any]] = []

    entries = re.split(r"\n(?=\s+\d+\.\s)", text)
    for entry in entries:
        lines = entry.strip().split("\n")
        if not lines:
            continue
        m = re.match(r"\s*(\d+)\.\s+(.+)", lines[0])
        if not m:
            continue

        rec: Dict[str, Any] = {"title": m.group(2).strip()}
        for line in (l.strip() for l in lines[1:] if l.strip()):
            if line.startswith("https://"):
                rec.setdefault("url", line)
            elif line.startswith("doi:"):
                rec.setdefault("url", f"https://doi.org/{line[4:]}")
            elif line.startswith('"'):
                rec["abstract"] = line.strip('"')
            elif "·" in line:
                parts = [p.strip() for p in line.split("·")]
                if parts:
                    rec["id"] = parts[0]
                if len(parts) >= 2:
                    rec["source"] = parts[1]
                if len(parts) >= 3:
                    rec["date"] = parts[2]
            elif "authors" not in rec:
                rec["authors"] = line

        if "title" in rec:
            records.append(rec)

    return records


def _parse_search_output(stdout: str) -> List[Dict[str, Any]]:
    """Parse paperclip search output. Tries JSON first; falls back to text
    parsing if --json is unsupported by the installed CLI/server.
    """
    text = _strip_ansi(stdout).strip()
    if not text:
        return []

    if text.startswith(("{", "[")):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("results") or data.get("papers") or data.get("data") or []
            else:
                items = []
            return [_normalize_record(item) for item in items if isinstance(item, dict)]
        except json.JSONDecodeError as exc:
            log(f"⚠ JSON parse failed despite JSON-like prefix: {exc}; falling back to text.")

    log("   (server returned text output; --json not honored, parsing text)")
    return _parse_search_text(text)


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(name: str, max_len: int = 120) -> str:
    """Make a string safe for use as a filename. Strips/replaces unsafe chars
    and truncates."""
    cleaned = _FILENAME_SAFE_RE.sub("_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    return cleaned[:max_len] or "paper"


_SEARCH_ID_RE = re.compile(r"\[(s_[0-9a-f]+)\]")


def _is_paperclip_error(text: str) -> bool:
    """`paperclip` writes errors as `ERR: ...` to stdout (rc still 0 in some
    cases). Treat any output beginning with that as a failure rather than a
    passage."""
    s = _strip_ansi(text).lstrip()
    return s.startswith("ERR:") or "ERR: vsh: cat:" in s


def extract_search_id(search_stdout: str) -> Optional[str]:
    """Find the search-result id (e.g. `s_60127c33`) embedded in the
    `Found N papers  [s_xxxxx]` header that paperclip prints.
    """
    m = _SEARCH_ID_RE.search(_strip_ansi(search_stdout))
    return m.group(1) if m else None


# ── MCP HTTP operations ──────────────────────────────────────────────────────
# Every command is routed through ``paperclip_http.call_paperclip`` (HTTPS POST
# to https://paperclip.gxl.ai/mcp) rather than shelling out to the CLI.

# Transient backend errors that should be retried (paperclip's hosted service
# occasionally returns these masked behind a generic message).
_SEARCH_TRANSIENT_RE = re.compile(
    r"Something went wrong|Please try again|temporar(?:y|ily)|timeout",
    re.IGNORECASE,
)


def _run_paperclip(args: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Submit a CLI-style ``paperclip`` command over the MCP HTTP transport.

    Joins argv into a single command string and returns ``(rc, stdout, stderr)``
    where ``rc`` matches the ``paperclip_http.RC_*`` constants.
    """
    command = " ".join(shlex.quote(a) for a in args)
    log(f"[mcp] paperclip {command}")
    return paperclip_http.call_paperclip(command, timeout=timeout)


def search(
    query: str, top_k: int = DEFAULT_TOP_K,
    source: Optional[str] = None, since: Optional[str] = None,
    retries: int = 3, retry_backoff: float = 1.0,
) -> Tuple[List[Dict[str, Any]], str, str, int]:
    """MCP-backed search.

    Returns ``(records, stdout, stderr, rc)``. Retries on paperclip-side
    transient errors. Non-transient failures (auth, 4xx) return immediately.
    The trailing ``rc`` lets ``_diagnose_search_failure`` classify why the
    search failed when ``records`` is empty.
    """
    args = ["search", query, "-n", str(top_k), "--json"]
    if source:
        args += ["--source", source]
    if since:
        args += ["--since", since]

    last_out, last_err, last_rc = "", "", 0
    for attempt in range(1, retries + 1):
        rc, out, err = _run_paperclip(args)
        last_out, last_err, last_rc = out, err, rc

        if rc == paperclip_http.RC_OK:
            records = _parse_search_output(out)
            log(f"   parsed {len(records)} records from search output"
                + (f" (attempt {attempt}/{retries})" if attempt > 1 else ""))
            return records, out, err, rc

        combined = (out or "") + "\n" + (err or "")
        transient = (
            bool(_SEARCH_TRANSIENT_RE.search(combined))
            or rc == paperclip_http.RC_BACKEND
        )
        if not transient or attempt >= retries:
            log(f"⚠ search failed (rc={rc}, attempt {attempt}/{retries}): "
                f"{(err or out).strip()[:200]}")
            return [], out, err, rc

        log(f"   transient search error on attempt {attempt}/{retries}; "
            f"retrying after {retry_backoff}s ...")
        time.sleep(retry_backoff)

    return [], last_out, last_err, last_rc


def fetch_full(
    paper_id: str,
    title: Optional[str] = None,
    save_dir: Optional[Path] = None,
) -> Tuple[str, Optional[Path], str]:
    """Fetch a paper's full text via ``paperclip cat --full``.

    Strips paperclip's trailing usage-hint block and writes the body to
    ``<save_dir>/<sanitized_title>.txt`` (default save_dir is ``SAVE_DIR``).

    Returns ``(text, saved_path, reason)``. ``reason`` is ``""`` on success or
    one of ``"slab_unavailable"``, ``"empty"``, ``"error"``.
    """
    rc, out, err = _run_paperclip(
        ["cat", "--full", f"/papers/{paper_id}/content.lines"],
        timeout=120,
    )
    text = _strip_ansi(out)

    if rc != paperclip_http.RC_OK:
        first = (err or text or "").strip().splitlines()[:1]
        log(f"   ⚠ fetch_full failed (rc={rc}): {first[0] if first else '<no output>'}")
        return "", None, "error"

    if _is_paperclip_error(text):
        first = text.strip().splitlines()[0] if text.strip() else ""
        reason = "slab_unavailable" if "slab" in first.lower() else "error"
        log(f"   ⚠ fetch_full: {first[:160]}")
        return "", None, reason

    if not text.strip():
        return "", None, "empty"

    # Trim trailing usage-hint lines paperclip appends after the body.
    lines = text.splitlines()
    cut = len(lines)
    for j in range(len(lines) - 1, -1, -1):
        line = lines[j]
        if line.startswith("  cat ") or line.startswith("  grep "):
            cut = j
        elif line.strip() == "" and cut == j + 1:
            cut = j
        else:
            break
    text = "\n".join(lines[:cut]).rstrip() + "\n"

    target_dir = (save_dir or SAVE_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    base = _sanitize_filename(title) if title else _sanitize_filename(paper_id)
    saved = target_dir / f"{base}.txt"
    try:
        saved.write_text(text, encoding="utf-8")
        log(f"   💾 full text ({len(text)} chars) saved to: {saved}")
    except OSError as exc:
        log(f"   ⚠ could not save full text: {exc}")
        saved = None

    return text, saved, ""


# ── LLM-grounded extraction ──────────────────────────────────────────────────

_LLM_SYSTEM_PROMPT = (
    "You are an extraction assistant. Answer the user's question STRICTLY from "
    "the paper text provided below. Quote section names, table numbers, and "
    "results only if they actually appear in the text. If the paper text does "
    "not contain enough information to answer, say so explicitly — do not "
    "invent components, baselines, datasets, metrics, or numerical results. "
    "Prefer concise, evidence-grounded answers over comprehensive-sounding "
    "ones."
)


def _build_openai_client(model: str = DEFAULT_LLM_MODEL) -> Tuple[Any, str]:
    """Build an OpenAI client honoring OPENAI_API_KEY / OPENAI_BASE_URL."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "openai package is required for LLM-grounded extraction; install "
            "with `pip install openai`"
        ) from exc

    api_key = os.environ.get("OPENAI_API_KEY") or "dummy_key"
    base_url = os.environ.get("OPENAI_BASE_URL")
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    return client, model


def extract_with_llm(
    paper_text: str,
    query: str,
    *,
    client: Any,
    model: str = DEFAULT_LLM_MODEL,
    paper_meta: Optional[Dict[str, Any]] = None,
    max_chars: int = DEFAULT_LLM_MAX_CHARS,
) -> Tuple[str, int, int]:
    """Ask `model` to answer `query` using `paper_text` as grounding.

    Returns ``(answer, prompt_tokens, completion_tokens)``. On API failure,
    returns an empty answer and zero tokens.

    Truncates `paper_text` to `max_chars` from the head — front-of-paper
    (abstract/intro/method) carries most of the signal for typical
    feedback-agent probes.
    """
    text = paper_text.strip()
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    meta_lines = []
    if paper_meta:
        for k in ("title", "authors", "date", "id"):
            v = paper_meta.get(k)
            if v:
                meta_lines.append(f"{k}: {v}")
    meta_block = "\n".join(meta_lines)

    user_msg = (
        f"PAPER METADATA:\n{meta_block}\n\n"
        f"PAPER TEXT{' (truncated)' if truncated else ''}:\n"
        f"---\n{text}\n---\n\n"
        f"QUESTION:\n{query}\n"
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001 — surface any API failure
        log(f"   ⚠ LLM call failed: {exc}")
        return "", 0, 0

    answer = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
    out_tok = getattr(usage, "completion_tokens", 0) or 0 if usage else 0
    return answer, in_tok, out_tok


# ── Retrieval pipeline ───────────────────────────────────────────────────────

def _build_paper_meta(record: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        "index": index,
        "id": (record.get("id") or "").strip(),
        "source": record.get("source", ""),
        "title": (record.get("title") or "").strip(),
        "abstract": (record.get("abstract") or "").strip(),
        "url": record.get("url", ""),
        "authors": record.get("authors", ""),
        "date": record.get("date", ""),
    }


def _empty_result(query: str, extraction_query: Optional[str],
                  error: Optional[str] = None) -> Dict[str, Any]:
    out = {
        "query": query,
        "papers": [],
        "global_top_chunks": [],
        "backend": "paperclip-mcp",
        "search_id": None,
        "extraction_query": extraction_query,
        "used_map": False,
        "extraction_backend": "none",
    }
    if error:
        out["error"] = error
    return out


def _diagnose_search_failure(rc: int, stdout: str, stderr: str) -> str:
    """Classify why an MCP-backed search returned no records.

    Numeric ``rc`` matches the ``paperclip_http.RC_*`` constants:
        RC_AUTH    (2)  – 401/403 from the MCP server
        RC_BACKEND (3)  – 5xx from the MCP server
        RC_CLIENT  (4)  – other 4xx
        RC_TIMEOUT (124)
        RC_UNKNOWN (1)  – anything else (incl. JSON-RPC error, isError=True)
    """
    err = (stderr or "").strip()
    out = (stdout or "").strip()
    combined = (out + "\n" + err).lower()

    if rc == paperclip_http.RC_AUTH:
        return (
            "paperclip MCP auth failed. Set PAPERCLIP_MCP_API_KEY in the "
            "environment to a valid key from https://paperclip.gxl.ai. "
            f"Server said: {err[:200] or out[:200]}"
        )
    if rc == paperclip_http.RC_TIMEOUT:
        return "paperclip MCP request timed out."
    if rc == paperclip_http.RC_BACKEND:
        return f"paperclip MCP backend transient error: {err[:200] or out[:200]}"
    if rc == paperclip_http.RC_CLIENT:
        return f"paperclip MCP rejected the request: {err[:200] or out[:200]}"

    # rc == RC_UNKNOWN / RC_OK with empty records — inspect the body.
    if "something went wrong" in combined or "please try again" in combined:
        return (
            "paperclip MCP returned a transient backend error after retries. "
            "Try again in a moment; if it persists, check paperclip status."
        )
    if "found 0 papers" in combined or "no papers" in combined:
        return "paperclip search returned 0 hits for this query."
    if "ERR:" in out or "ERR:" in err:
        head = [ln for ln in (err + "\n" + out).splitlines() if "ERR:" in ln][:1]
        return f"paperclip error: {(head[0] if head else 'unknown')[:300]}"
    head = [ln for ln in out.splitlines() if ln.strip()][:1]
    return (f"paperclip MCP returned no records. "
            f"First stdout line: {(head[0] if head else '<empty>')[:200]}")


def _extract_llm_passages(
    extraction_query: str,
    papers: List[Dict[str, Any]],
    *,
    client: Any,
    model: str,
    max_chars: int,
    save_dir: Optional[Path] = None,
) -> Dict[str, List[str]]:
    """Extract per-paper answer passages from full text via an LLM.

    For each paper: ``fetch_full`` (HTTP-transport) → ``extract_with_llm``
    (gpt-4o-mini by default, grounded on the fetched text). Skips papers
    whose body is unavailable (e.g. slab service down) instead of
    hallucinating an answer from the title alone.

    Returns ``{paper_id: [answer, ...]}``.
    """
    by_paper: Dict[str, List[str]] = {}
    unavailable: Dict[str, str] = {}
    in_tok_sum = 0
    out_tok_sum = 0

    for p in papers:
        pid = p.get("id", "")
        if not pid:
            continue
        text, _path, reason = fetch_full(
            pid, title=p.get("title"), save_dir=save_dir,
        )
        if not text:
            unavailable[pid] = reason or "unknown"
            continue

        answer, in_tok, out_tok = extract_with_llm(
            text, extraction_query,
            client=client, model=model,
            paper_meta=p, max_chars=max_chars,
        )
        in_tok_sum += in_tok
        out_tok_sum += out_tok
        if answer:
            by_paper[pid] = [answer]

    if unavailable:
        log(f"   ⚠ LLM extraction skipped for {len(unavailable)} paper(s): {unavailable}")
    log(f"   LLM extraction tokens in/out: {in_tok_sum}/{out_tok_sum}")
    return by_paper


def paper_retrieval_pipeline(
    search_query: str,
    extraction_query: Optional[str] = None,
    top_k: int = 3,
    *,
    llm_model: str = DEFAULT_LLM_MODEL,
    llm_max_chars: int = DEFAULT_LLM_MAX_CHARS,
    save_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Search Paperclip for papers and, optionally, extract a grounded passage.

    Args:
        search_query: topical query for paper retrieval. ≤12 words works best.
        extraction_query: optional question; when provided, each paper's full
            text is fetched and read by an LLM to extract a relevant passage.
            When omitted, abstracts are returned as ``top_chunks`` (cheaper,
            faster).
        top_k: number of papers to retrieve.

    Returns:
        ``{"query", "papers": [{"index","title","abstract","top_chunks",...}],
            "global_top_chunks": [str, ...], "backend": "paperclip-mcp",
            "search_id", "extraction_query", "used_map"}``. On failure the
        dict also carries ``"error"`` with a diagnostic message.
    """
    records, search_stdout, search_stderr, search_rc = search(
        search_query, top_k=top_k, source="arxiv",
    )
    if not records:
        diag = _diagnose_search_failure(search_rc, search_stdout, search_stderr)
        return _empty_result(search_query, extraction_query, error=diag)

    search_id = extract_search_id(search_stdout)

    papers: List[Dict[str, Any]] = [
        _build_paper_meta(r, i + 1) for i, r in enumerate(records[:top_k])
    ]

    used_extraction = False
    extraction_backend = "none"
    map_passages: Dict[str, List[str]] = {}
    if extraction_query and extraction_query.strip():
        if not os.environ.get("OPENAI_API_KEY"):
            log("⚠ extraction_query supplied but OPENAI_API_KEY is unset; "
                "falling back to abstracts. Set OPENAI_API_KEY (and "
                "optionally OPENAI_BASE_URL) to enable LLM-grounded extraction.")
        else:
            client, model = _build_openai_client(model=llm_model)
            map_passages = _extract_llm_passages(
                extraction_query.strip(), papers,
                client=client, model=model,
                max_chars=llm_max_chars, save_dir=save_dir or SAVE_DIR,
            )
            used_extraction = True
            extraction_backend = f"llm:{model}"

    # Assemble per-paper top_chunks. LLM-extracted passages take precedence;
    # otherwise fall back to the abstract so the slot is never empty.
    for p in papers:
        chunks = map_passages.get(p["id"], []) if used_extraction else []
        if not chunks and p["abstract"]:
            chunks = [p["abstract"]]
        p["top_chunks"] = chunks

    global_top_chunks: List[str] = []
    for p in papers:
        global_top_chunks.extend(p["top_chunks"])

    return {
        "query": search_query,
        "papers": papers,
        "global_top_chunks": global_top_chunks,
        "backend": "paperclip-mcp",
        "search_id": search_id,
        "extraction_query": extraction_query,
        # `used_map`: whether per-paper LLM extraction ran for this query.
        # See `extraction_backend` for which backend produced the passages.
        "used_map": used_extraction,
        "extraction_backend": extraction_backend,
    }


# ── CLI smoke test ───────────────────────────────────────────────────────────

def main() -> int:
    import argparse

    from dotenv import load_dotenv
    load_dotenv()  # load a local .env if present; otherwise env vars are read directly

    _setup_main_logging()

    parser = argparse.ArgumentParser(
        description="Paperclip retrieval smoke test: MCP HTTP search + optional "
                    "LLM-grounded extraction (gpt-4o-mini by default).",
    )
    parser.add_argument("--query", default=DEFAULT_QUERY,
                        help="Search query for paper retrieval.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--extraction-query", default=None,
                        help="Optional per-paper LLM extraction question.")
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL,
                        help=f"OpenAI model (default: {DEFAULT_LLM_MODEL}).")
    parser.add_argument("--llm-max-chars", type=int, default=DEFAULT_LLM_MAX_CHARS,
                        help="Max paper-text chars sent per LLM call "
                             f"(default: {DEFAULT_LLM_MAX_CHARS}).")
    parser.add_argument("--save-dir", default=str(SAVE_DIR),
                        help=f"Directory for full-text dumps + result JSON "
                             f"(default: {SAVE_DIR}).")
    args = parser.parse_args()

    if not os.environ.get("PAPERCLIP_MCP_API_KEY"):
        log("⚠ PAPERCLIP_MCP_API_KEY is unset; the MCP HTTP transport will "
            "likely fail with 401.")

    save_dir = Path(args.save_dir).resolve()
    save_dir.mkdir(parents=True, exist_ok=True)

    log(f"\n🔍 paperclip search: {args.query!r}  (top_k={args.top_k})")
    pipeline_out = paper_retrieval_pipeline(
        search_query=args.query,
        extraction_query=args.extraction_query,
        top_k=args.top_k,
        llm_model=args.llm_model,
        llm_max_chars=args.llm_max_chars,
        save_dir=save_dir,
    )

    if pipeline_out.get("error"):
        log(f"\n✗ search failed: {pipeline_out['error']}")
        return 1

    log("\n── Paper-level summary ──")
    log(f"extraction_backend: {pipeline_out.get('extraction_backend')}")
    for p in pipeline_out["papers"]:
        log(f"  [{p['index']}] {p['source']:>10s}  {p['id']:<24s}  {p['title'][:80]}")

    out_path = save_dir / "paperclip_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(pipeline_out, f, ensure_ascii=False, indent=2)
    log(f"\n✅ results written to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
