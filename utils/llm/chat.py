"""LLM communication functions."""

import os
from typing import Optional, Tuple

from openai import OpenAI
from docling_core.types.doc import TextItem


def openai_chat_text(
    client: OpenAI,
    model: str,
    user_prompt: str,
    system_prompt: Optional[str] = None,
    prefer_responses: bool = True,
) -> Tuple[str, int, int]:
    """
    Unified text generation helper that works with:
      - Official OpenAI endpoints (supports Responses API), and
      - Many relay/gateway providers (often only support Chat Completions).

    Strategy:
      1) Try Responses API first (POST /v1/responses) if prefer_responses is True.
      2) If it fails for any reason (404/503/non-standard gateway behavior), fall back
         to Chat Completions (POST /v1/chat/completions).

    Returns:
      (text, prompt_tokens, completion_tokens)

    Tip for gateway compatibility:
      If your gateway does NOT support /v1/responses, force chat-only mode:
        export OPENAI_FORCE_CHAT=1
    """
    # If the gateway doesn't support /v1/responses, force fallback to chat.completions.
    force_chat = os.getenv("OPENAI_FORCE_CHAT", "").strip().lower() in {"1", "true", "yes"}
    if force_chat:
        prefer_responses = False

    # ----------------------------
    # 1) Try Responses API (newer)
    # ----------------------------
    if prefer_responses:
        try:
            # In Responses API, the "system" content is passed via `instructions`.
            resp = client.responses.create(
                model=model,
                input=user_prompt,
                instructions=system_prompt or None,
                reasoning={"effort": "minimal"},
                text={"verbosity": "low"},
            )

            # Best-case: SDK provides output_text directly.
            text = getattr(resp, "output_text", None)

            # Fallback: if your project already has extract_text_from_responses(resp),
            # use it to stay consistent with existing parsing.
            if not text:
                extractor = globals().get("extract_text_from_responses", None)
                if callable(extractor):
                    text = extractor(resp)
                else:
                    # Last-resort: parse the structured output manually.
                    out = getattr(resp, "output", None) or []
                    parts = []
                    for item in out:
                        content = getattr(item, "content", None) or []
                        for c in content:
                            t = getattr(c, "text", None)
                            if t:
                                parts.append(t)
                    text = "\n".join(parts).strip()

            # Token usage fields in Responses API.
            usage = getattr(resp, "usage", None)
            prompt_tokens = getattr(usage, "input_tokens", 0) if usage else 0
            completion_tokens = getattr(usage, "output_tokens", 0) if usage else 0
            return (text or "").strip(), int(prompt_tokens), int(completion_tokens)

        except Exception:
            # Many relay gateways either:
            #  - don't implement /v1/responses at all, or
            #  - return non-standard errors/bodies.
            # In that case, fall back to chat.completions.
            pass

    # --------------------------------
    # 2) Fallback: Chat Completions API
    # --------------------------------
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    cc = client.chat.completions.create(
        model=model,
        messages=messages,
    )

    text = (cc.choices[0].message.content or "").strip()
    usage = getattr(cc, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
    completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
    return text, int(prompt_tokens), int(completion_tokens)


def chat_via_vllm(prompt, actor_config, actor_model, actor_sys_msg) -> str:
    model_name = actor_config.get("model") or actor_config.get("model_type")
    # Prefer the base URL from config; fall back to the VLLM_BASE_URL env var,
    # then a local default.
    base_url = (
        actor_config.get("url")
        or os.environ.get("VLLM_BASE_URL", "").strip()
        or "http://localhost:8000/v1"
    )
    api_key = actor_config.get("api_key") or os.environ.get("OPENAI_API_KEY", "testkey")
    client = OpenAI(base_url=base_url, api_key=api_key)
    actor_model._client = client
    resp = actor_model._client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": actor_sys_msg},
            {"role": "user", "content": prompt},
        ],
    )
    return resp


def get_page_text(raw_result, page_no):
    texts = [
        item.text
        for item in raw_result.document.texts
        if isinstance(item, TextItem) and item.prov[0].page_no == page_no
    ]
    return texts


def account_token(response):
    input_token = response.info['usage']['prompt_tokens']
    output_token = response.info['usage']['completion_tokens']

    return input_token, output_token


def extract_text_from_responses(resp) -> str:
    """Extract text content from an OpenAI Responses API response object."""
    txt = getattr(resp, "output_text", None)
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    parts = []
    for item in getattr(resp, "output", []) or []:
        content = getattr(item, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, str):
                    parts.append(blk)
                elif isinstance(blk, dict):
                    parts.append(blk.get("text") or blk.get("content") or "")
                else:
                    parts.append(getattr(blk, "text", "") or getattr(blk, "content", ""))
    txt = "".join(parts).strip()
    if not txt:
        raise RuntimeError("empty_response_text")
    return txt
