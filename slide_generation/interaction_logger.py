"""Central interaction logger.

Captures every agent call (input/output/tokens/timing/caller) and every
user feedback event into a JSONL file. Enable once at pipeline startup
via ``init_interaction_logger(path)``.

Strategy: monkey-patch the OpenAI SDK's *class-level* ``Responses.create``
and ``chat.completions.Completions.create``. Every OpenAI client instance
in the codebase — including those used by ``utils.llm.chat.openai_chat_text``,
Camel ``ChatAgent``, ``chat_via_vllm``, and direct ``client.*.create(...)``
calls in the RST / summarization / figure-matcher / layout-planner /
plan-refiner / speaker-notes / feedback modules — routes through these
class methods, so a single patch catches everything.

Caller context (filename + function + line) is recorded with each agent
record to make the dataset useful for per-agent fine-tuning.

"""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


_lock = threading.Lock()
_state: Dict[str, Any] = {
    "enabled": False,
    "path": None,
    "patched": False,
}


# Mapping: caller file stem → human-readable agent name.
# Lets downstream fine-tuning datasets filter by agent role instead of by
# transport endpoint. When a caller's file stem is not in this table, we fall
# back to the stem itself.
_AGENT_NAME_BY_STEM: Dict[str, str] = {
    # outline_generation
    "summarization": "section_summarizer",
    "integration": "rst_integrator",
    "section_discourse_parser": "rst_parser",
    "slide_reviser": "outline_reviser",
    "slide_planner": "slide_planner",
    "narrative_critic": "narrative_critic",
    "providers": "outline_llm_provider",
    # content_generation
    "plan_refiner": "plan_refiner",
    "layout_planner": "layout_planner",
    "figure_matcher": "figure_matcher",
    # renderer / JS codegen
    "js_codegen": "js_codegen",
    "js_renderer": "js_renderer",
    # pipeline entry points
    "pipeline": "pipeline_driver",
}


def _derive_agent_name(caller: Optional[Dict[str, Any]]) -> str:
    """Map a caller frame to a semantic agent name.

    ``caller`` comes from ``_caller_info()`` and has keys {file, function,
    line, module}. ``module`` is the file stem. Falls back to the stem when
    unknown, and to ``"unknown_agent"`` if no caller info is available.
    """
    if not caller:
        return "unknown_agent"
    stem = caller.get("module") or ""
    if stem in _AGENT_NAME_BY_STEM:
        return _AGENT_NAME_BY_STEM[stem]
    if "camel" in (caller.get("file") or ""):
        return "camel_chat_agent"
    return stem or "unknown_agent"


def _truncate(value: Any, limit: int = 200000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...[truncated {len(value) - limit} chars]"
    return value


def _write(record: Dict[str, Any]) -> None:
    path = _state.get("path")
    if not path:
        return
    record.setdefault("timestamp", time.strftime("%Y-%m-%d %H:%M:%S"))
    record.setdefault("epoch", time.time())
    try:
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        print(f"[interaction_logger] write failed: {exc}")


def is_enabled() -> bool:
    return bool(_state.get("enabled"))


def _caller_info(skip: int = 3) -> Dict[str, Any]:
    """Walk the stack past internal frames to find the user-code caller."""
    try:
        stack = inspect.stack()
    except Exception:
        return {}
    this_file = os.path.abspath(__file__)
    for frame in stack[skip:]:
        fname = os.path.abspath(frame.filename)
        if fname == this_file:
            continue
        if "/openai/" in fname or "\\openai\\" in fname:
            continue
        if "/camel/" in fname and "ConvDeck/camel/" not in fname:
            # Skip installed camel package internals; keep our vendored one
            continue
        module = Path(fname).stem
        parent = Path(fname).parent.name
        return {
            "file": f"{parent}/{Path(fname).name}",
            "function": frame.function,
            "line": frame.lineno,
            "module": module,
        }
    return {}


def log_agent_interaction(
    agent_name: str,
    user_input: Any,
    output: Any,
    system_prompt: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration: Optional[float] = None,
    model: Optional[str] = None,
    caller: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    if not is_enabled():
        return
    record: Dict[str, Any] = {
        "kind": "agent",
        "agent": agent_name,
        "agent_name": _derive_agent_name(caller),
        "model": model,
        "system_prompt": _truncate(system_prompt) if system_prompt else None,
        "input": _truncate(user_input) if isinstance(user_input, str) else user_input,
        "output": _truncate(output) if isinstance(output, str) else output,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_sec": duration,
    }
    if caller:
        record["caller"] = caller
    if extra:
        record["extra"] = extra
    _write(record)


def log_user_feedback(
    stage: str,
    content: Any,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    if not is_enabled():
        return
    record: Dict[str, Any] = {
        "kind": "user_feedback",
        "stage": stage,
        "content": _truncate(content) if isinstance(content, str) else content,
    }
    if metadata:
        record["metadata"] = metadata
    _write(record)


# ── OpenAI SDK patching ─────────────────────────────────────────────────────

def _msg_role_content(m: Any) -> tuple[Optional[str], Any]:
    if isinstance(m, dict):
        return m.get("role"), m.get("content")
    return getattr(m, "role", None), getattr(m, "content", None)


def _flatten_messages(messages: Any) -> tuple[Optional[str], str]:
    """Split messages into (system_prompt, user_turns_joined)."""
    if not messages:
        return None, ""
    system_parts: List[str] = []
    other_parts: List[str] = []
    for m in messages:
        role, content = _msg_role_content(m)
        if content is None:
            content = ""
        if isinstance(content, list):
            # OpenAI content can be list of {type,text,image_url...}
            text_bits = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text":
                        text_bits.append(c.get("text", ""))
                    elif c.get("type") == "image_url":
                        url = (c.get("image_url") or {}).get("url", "")
                        text_bits.append(f"[image_url: {url[:120]}]")
                    else:
                        text_bits.append(json.dumps(c, default=str)[:500])
                else:
                    text_bits.append(str(c))
            content = "\n".join(text_bits)
        if role == "system":
            system_parts.append(str(content))
        else:
            other_parts.append(f"[{role}] {content}")
    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return system_prompt, "\n\n".join(other_parts)


def _extract_responses_output(resp: Any) -> Any:
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    parts: List[str] = []
    try:
        for item in getattr(resp, "output", []) or []:
            content = getattr(item, "content", None)
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for blk in content:
                    t = getattr(blk, "text", None) or (blk.get("text") if isinstance(blk, dict) else None)
                    if t:
                        parts.append(t)
    except Exception:
        pass
    return "\n".join(parts) if parts else str(resp)[:2000]


def _extract_completion_output(resp: Any) -> Any:
    try:
        choice = resp.choices[0]
        msg = getattr(choice, "message", None)
        text = getattr(msg, "content", None) if msg else None
        tool_calls = getattr(msg, "tool_calls", None) if msg else None
        if tool_calls:
            return {
                "content": text,
                "tool_calls": [
                    {
                        "name": getattr(tc.function, "name", None),
                        "arguments": getattr(tc.function, "arguments", None),
                    }
                    for tc in tool_calls
                ],
            }
        return text or ""
    except Exception:
        return str(resp)[:2000]


def _patch_openai_sdk() -> None:
    """Patch ``Responses.create`` and ``Completions.create`` at the class level."""
    try:
        from openai.resources.responses import Responses
    except Exception as exc:
        Responses = None  # type: ignore
        print(f"[interaction_logger] openai Responses not importable: {exc}")

    try:
        from openai.resources.chat.completions import Completions
    except Exception as exc:
        Completions = None  # type: ignore
        print(f"[interaction_logger] openai Completions not importable: {exc}")

    # ── Responses.create ───────────────────────────────────────────────────
    if Responses is not None and not getattr(Responses.create, "_interaction_logged", False):
        _orig_resp_create = Responses.create

        def _logged_resp_create(self, *args, **kwargs):  # type: ignore[no-redef]
            if not is_enabled():
                return _orig_resp_create(self, *args, **kwargs)
            caller = _caller_info()
            start = time.time()
            resp = _orig_resp_create(self, *args, **kwargs)
            dt = time.time() - start
            try:
                if kwargs.get("stream"):
                    # Streams are iterators; skip consuming. Record intent only.
                    log_agent_interaction(
                        agent_name="openai.responses",
                        user_input=kwargs.get("input"),
                        output="[streamed — not captured]",
                        system_prompt=kwargs.get("instructions"),
                        duration=dt,
                        model=str(kwargs.get("model")),
                        caller=caller,
                    )
                    return resp

                usage = getattr(resp, "usage", None)
                in_tok = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
                out_tok = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0

                log_agent_interaction(
                    agent_name="openai.responses",
                    user_input=kwargs.get("input"),
                    output=_extract_responses_output(resp),
                    system_prompt=kwargs.get("instructions"),
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    duration=dt,
                    model=str(kwargs.get("model")),
                    caller=caller,
                )
            except Exception as exc:
                print(f"[interaction_logger] responses logging failed: {exc}")
            return resp

        _logged_resp_create._interaction_logged = True
        Responses.create = _logged_resp_create

    # ── chat.completions.Completions.create ────────────────────────────────
    if Completions is not None and not getattr(Completions.create, "_interaction_logged", False):
        _orig_cc_create = Completions.create

        def _logged_cc_create(self, *args, **kwargs):  # type: ignore[no-redef]
            if not is_enabled():
                return _orig_cc_create(self, *args, **kwargs)
            caller = _caller_info()
            start = time.time()
            resp = _orig_cc_create(self, *args, **kwargs)
            dt = time.time() - start
            try:
                if kwargs.get("stream"):
                    messages = kwargs.get("messages") or []
                    sys_p, user_in = _flatten_messages(messages)
                    log_agent_interaction(
                        agent_name="openai.chat.completions",
                        user_input=user_in,
                        output="[streamed — not captured]",
                        system_prompt=sys_p,
                        duration=dt,
                        model=str(kwargs.get("model")),
                        caller=caller,
                    )
                    return resp

                messages = kwargs.get("messages") or []
                sys_p, user_in = _flatten_messages(messages)
                usage = getattr(resp, "usage", None)
                in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
                out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0

                extra: Dict[str, Any] = {}
                tools = kwargs.get("tools")
                if tools:
                    extra["tools_offered"] = [
                        (t.get("function") or {}).get("name")
                        for t in tools if isinstance(t, dict)
                    ]

                log_agent_interaction(
                    agent_name="openai.chat.completions",
                    user_input=user_in,
                    output=_extract_completion_output(resp),
                    system_prompt=sys_p,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    duration=dt,
                    model=str(kwargs.get("model")),
                    caller=caller,
                    extra=extra or None,
                )
            except Exception as exc:
                print(f"[interaction_logger] chat.completions logging failed: {exc}")
            return resp

        _logged_cc_create._interaction_logged = True
        Completions.create = _logged_cc_create


def init_interaction_logger(path: str) -> None:
    """Enable logging and route records to *path* (JSONL, fresh per run).

    If *path* already exists from a prior run, it is rotated to
    ``<stem>.<N><suffix>`` where N is the next free integer starting at 0,
    so earlier runs' logs (including cached-stage records that won't be
    regenerated this run) are preserved.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size > 0:
        n = 0
        while p.with_suffix(f".{n}{p.suffix}").exists():
            n += 1
        p.rename(p.with_suffix(f".{n}{p.suffix}"))
    p.write_text("", encoding="utf-8")
    _state["path"] = str(p)
    _state["enabled"] = True

    _write({
        "kind": "session_start",
        "path": str(p),
        "pid": os.getpid(),
    })

    if not _state.get("patched"):
        _patch_openai_sdk()
        _state["patched"] = True

    print(f"[interaction_logger] Logging all OpenAI API calls and user feedback → {p}")


def close_interaction_logger() -> None:
    if is_enabled():
        _write({"kind": "session_end"})
    _state["enabled"] = False
