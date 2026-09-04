"""
Minimal HTTP client for the Paperclip MCP endpoint.

Sends a direct POST to ``https://paperclip.gxl.ai/mcp``. The MCP server exposes
a single tool named ``paperclip`` that takes a Paperclip CLI command verbatim
as a ``command`` string and returns the same text the CLI would have printed,
so this module is a pure transport: same input, same output, with no
subprocess, TTY, or CLI auth state.

Auth: reads ``PAPERCLIP_MCP_API_KEY`` from the environment and sends it as the
``X-API-Key`` header. This is intentionally a different variable from the
Paperclip CLI's ``PAPERCLIP_API_KEY`` so the HTTP and CLI paths never share or
clobber each other's credentials.

The endpoint is stateless for ``tools/call`` — no ``initialize`` handshake or
``Mcp-Session-Id`` is required — so each request is a single POST.
"""

from __future__ import annotations

import json
import os
import re
from typing import Tuple

try:
    import httpx
except ImportError as _e:  # pragma: no cover - import-time guard
    raise ImportError(
        "paperclip_http requires the `httpx` package. Install with "
        "`pip install httpx`."
    ) from _e


MCP_URL = "https://paperclip.gxl.ai/mcp"
MCP_PROTOCOL_VERSION = "2025-03-26"

# Map our internal return-code convention onto HTTP/transport failures so the
# diagnostic code downstream (paperclip_search._diagnose_search_failure)
# can switch on numeric rc to tell auth/timeout/backend failures apart.
RC_OK = 0
RC_AUTH = 2          # 401/403 — bad or missing X-API-Key
RC_BACKEND = 3       # 5xx — paperclip server side
RC_CLIENT = 4        # 4xx other — bad request / not found
RC_TIMEOUT = 124     # network timeout (matches subprocess.TimeoutExpired)
RC_UNKNOWN = 1       # anything else


# paperclip's MCP responses may arrive either as plain JSON or wrapped in a
# single SSE ``event: message\ndata: {...}`` frame when the client advertises
# ``text/event-stream``. We tolerate both.
_SSE_DATA_RE = re.compile(r"^data:\s*(.*)$", re.MULTILINE)


def _parse_mcp_body(raw: str) -> dict:
    """Parse the response body, handling SSE-framed and plain JSON both."""
    text = raw.strip()
    if not text:
        return {}
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    # SSE frame: pick the last data: line (paperclip sends one per response).
    data_lines = _SSE_DATA_RE.findall(text)
    if data_lines:
        return json.loads(data_lines[-1])
    raise ValueError(f"unrecognized MCP response body: {text[:200]!r}")


def call_paperclip(
    command: str,
    timeout: int = 120,
    *,
    api_key: str | None = None,
) -> Tuple[int, str, str]:
    """Run a paperclip CLI command via the MCP endpoint.

    Returns ``(rc, stdout, stderr)`` — the convention the ``paperclip_search``
    operations build on.

    ``stdout`` is the concatenation of the text blocks returned in
    ``result.content``. ``stderr`` is empty on success; on transport or
    tool-level failure it holds the error message.
    """
    key = api_key or os.environ.get("PAPERCLIP_MCP_API_KEY")
    if not key:
        return (RC_AUTH, "",
                "PAPERCLIP_MCP_API_KEY not set in environment. Get a key from "
                "https://paperclip.gxl.ai and add it to your .env (or shell).")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "paperclip", "arguments": {"command": command}},
    }
    headers = {
        "X-API-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    try:
        resp = httpx.post(MCP_URL, headers=headers, json=payload, timeout=timeout)
    except httpx.TimeoutException:
        return RC_TIMEOUT, "", f"paperclip MCP request timed out after {timeout}s"
    except httpx.HTTPError as e:
        return RC_UNKNOWN, "", f"paperclip MCP HTTP error: {e}"

    status = resp.status_code
    body = resp.text

    if status == 401 or status == 403:
        return RC_AUTH, "", (
            f"paperclip MCP auth failed (HTTP {status}). Check that "
            f"PAPERCLIP_MCP_API_KEY is a valid key from "
            f"https://paperclip.gxl.ai. Server said: {body[:300]}"
        )
    if 500 <= status < 600:
        return RC_BACKEND, "", (
            f"paperclip MCP backend error (HTTP {status}): {body[:300]}"
        )
    if status >= 400:
        return RC_CLIENT, "", (
            f"paperclip MCP request rejected (HTTP {status}): {body[:300]}"
        )

    try:
        parsed = _parse_mcp_body(body)
    except (json.JSONDecodeError, ValueError) as e:
        return RC_UNKNOWN, "", f"could not parse MCP response: {e}; body: {body[:300]}"

    # JSON-RPC error envelope.
    if "error" in parsed and parsed["error"]:
        err = parsed["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return RC_UNKNOWN, "", f"paperclip MCP error: {msg}"

    result = parsed.get("result") or {}
    content_items = result.get("content") or []
    text_parts: list[str] = []
    for item in content_items:
        if isinstance(item, dict) and item.get("type") == "text":
            text_parts.append(item.get("text", ""))
    stdout = "\n".join(text_parts)

    # Tool-level error: result.isError == True. The text content usually
    # carries the actual error message in this case.
    if result.get("isError"):
        return RC_UNKNOWN, "", stdout or "paperclip tool reported isError=True"

    return RC_OK, stdout, ""


def initialize_check(timeout: int = 30, *, api_key: str | None = None) -> Tuple[bool, str]:
    """One-shot sanity check: does ``initialize`` succeed with the current key?

    Not required for normal operation (paperclip's MCP server accepts
    stateless ``tools/call`` requests), but useful as a startup probe and for
    surfacing a clear "your key is bad" error before the first real query.

    Returns ``(ok, message)``.
    """
    key = api_key or os.environ.get("PAPERCLIP_MCP_API_KEY")
    if not key:
        return False, "PAPERCLIP_MCP_API_KEY not set"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "convdeck", "version": "0.1"},
        },
    }
    headers = {
        "X-API-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        resp = httpx.post(MCP_URL, headers=headers, json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if resp.status_code in (401, 403):
        return False, f"auth failed (HTTP {resp.status_code}): {resp.text[:200]}"
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        parsed = _parse_mcp_body(resp.text)
    except (json.JSONDecodeError, ValueError) as e:
        return False, f"parse error: {e}"
    server_info = (parsed.get("result") or {}).get("serverInfo") or {}
    name = server_info.get("name", "?")
    version = server_info.get("version", "?")
    return True, f"connected to {name} v{version}"
