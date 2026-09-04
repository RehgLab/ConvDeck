#!/usr/bin/env python3
"""
Paper text summarizer (Markdown → condensed Markdown)
=====================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from utils.llm.chat import extract_text_from_responses

from .slide_planner import SlidePlannerConfig
from .providers import get_provider, load_prompt


# =============================================================================
# Chat API helpers (mirror integration.py; kept here to avoid import cycles)
# =============================================================================


def _completion_token_kwargs(model_name: str, max_tokens: int) -> dict:
    if model_name and "gpt-5" in str(model_name).lower():
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _completion_kwargs(model_name: str, max_tokens: int, temperature: float) -> dict:
    kwargs = _completion_token_kwargs(model_name, max_tokens)
    if not (model_name and "gpt-5" in str(model_name).lower()):
        kwargs["temperature"] = temperature
    return kwargs


# =============================================================================
# Token accumulation
# =============================================================================


class _TokenAccumulator:
    """Accumulates input/output token counts across multiple LLM calls."""

    def __init__(self):
        self.total_in = 0
        self.total_out = 0

    def add(self, in_tok: int, out_tok: int) -> None:
        self.total_in += in_tok
        self.total_out += out_tok


def _extract_tokens_from_response(response, is_gpt5: bool) -> Tuple[int, int]:
    """Extract (input_tokens, output_tokens) from an API response."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0

    if is_gpt5:
        in_t = getattr(usage, "input_tokens", 0) or 0
        out_t = getattr(usage, "output_tokens", 0) or 0
    else:
        in_t = getattr(usage, "prompt_tokens", 0) or 0
        out_t = getattr(usage, "completion_tokens", 0) or 0

    return in_t, out_t


# =============================================================================
# Config
# =============================================================================


@dataclass
class PaperSummarizerConfig:
    enabled: bool = True
    save_intermediate: bool = True
    compression_hint: str = "30–50%"
    max_chars_per_chunk: int = 28000


SUMMARIZER_PROMPT = load_prompt("paper_summarizer.txt")


# =============================================================================
# Helpers
# =============================================================================


def _strip_markdown_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n([\s\S]*?)\n```\s*$", t)
    if m:
        return m.group(1).strip()
    m2 = re.match(r"^```(?:markdown|md)?\s*\n([\s\S]*)", t)
    if m2 and t.rstrip().endswith("```"):
        inner = m2.group(1)
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3].rstrip()
        return inner.strip()
    return t


_SUBSECTION_HEADING_RE = re.compile(r"^## \d+\.\d+")
_MAJOR_NUMBERED_SECTION_RE = re.compile(r"^## \d+\s")


def _is_subsection_heading_line(line: str) -> bool:
    return bool(_SUBSECTION_HEADING_RE.match(line.strip()))


def _is_major_numbered_section_line(line: str) -> bool:
    return bool(_MAJOR_NUMBERED_SECTION_RE.match(line.strip()))


def _split_paper_sections(md: str) -> List[str]:
    lines = md.splitlines(keepends=True)
    blocks: List[str] = []
    current: List[str] = []
    seen_major_numbered = False

    for line in lines:
        if line.startswith("## ") and current:
            if _is_subsection_heading_line(line):
                current.append(line)
            elif _is_major_numbered_section_line(line):
                blocks.append("".join(current).rstrip())
                current = [line]
                seen_major_numbered = True
            elif not seen_major_numbered:
                current.append(line)
            else:
                blocks.append("".join(current).rstrip())
                current = [line]
        else:
            current.append(line)

    if current:
        blocks.append("".join(current).rstrip())
    return [b for b in blocks if b.strip()]


def _split_oversized_block(block: str, max_chars: int) -> List[tuple[str, str]]:
    if len(block) <= max_chars:
        return [("", block)]

    paras = re.split(r"\n{2,}", block)
    cont_first = (
        "NOTE: This is one major paper section. The input may contain several "
        "``##`` lines (subsections such as ``## 3.1``). Preserve those headings "
        "and order in your condensed output."
    )
    cont_next = (
        "NOTE: Continuation of the SAME major section (more ``##`` subsections "
        "and body). Do not repeat content you already condensed in an earlier "
        "chunk of this section; only cover what appears below."
    )

    chunks: List[tuple[str, str]] = []
    buf: List[str] = []
    buf_len = 0
    is_first = True

    def flush() -> None:
        nonlocal buf, buf_len, is_first
        if not buf:
            return
        text = "\n\n".join(buf).strip()
        instr = cont_first if is_first else cont_next
        chunks.append((instr, text))
        is_first = False
        buf = []
        buf_len = 0

    for p in paras:
        p = p.strip()
        if not p:
            continue
        sep = 2 if buf else 0
        if buf and buf_len + sep + len(p) > max_chars:
            flush()
        buf.append(p)
        buf_len += sep + len(p)
    flush()
    return chunks if chunks else [("", block)]


class _ModelFactorySummarizerProvider:
    """LLM calls via ModelFactory client (same pattern as integration.py)."""

    def __init__(
        self,
        llm_config: Any,
        client: Any,
        model_name: str,
        system_message: str,
        accumulator: Optional[_TokenAccumulator] = None,
    ):
        self.config = llm_config
        self.client = client
        self.model_name = model_name
        self.system_message = system_message
        self._accumulator = accumulator

    def generate(self, prompt: str) -> Optional[str]:
        is_gpt5 = self.model_name and "gpt-5" in str(self.model_name).lower()

        if is_gpt5:
            response = self.client.responses.create(
                model=self.model_name,
                input=prompt,
                instructions=self.system_message,
                reasoning={"effort": "minimal"},
                text={"verbosity": "low"},
            )
            text = extract_text_from_responses(response)
        else:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": self.system_message},
                    {"role": "user", "content": prompt},
                ],
                **_completion_kwargs(
                    self.model_name,
                    int(getattr(self.config, "max_tokens", 4096)),
                    float(getattr(self.config, "temperature", 0.1)),
                ),
            )
            text = response.choices[0].message.content

        if self._accumulator is not None:
            in_t, out_t = _extract_tokens_from_response(response, is_gpt5)
            self._accumulator.add(in_t, out_t)

        return text


def _create_provider_via_model_factory(
    config: Any,
    system_message: str,
    accumulator: Optional[_TokenAccumulator] = None,
) -> _ModelFactorySummarizerProvider:
    from camel.models import ModelFactory

    agent_config = getattr(config, "agent_config", None)
    if not isinstance(agent_config, dict) or not agent_config.get("model_platform"):
        raise ValueError("config.agent_config missing or incomplete for ModelFactory")

    model_kwargs: Dict[str, Any] = {
        "model_platform": agent_config["model_platform"],
        "model_type": agent_config["model_type"],
        "model_config_dict": agent_config.get("model_config", {}),
    }
    if "url" in agent_config:
        model_kwargs["url"] = agent_config["url"]

    model = ModelFactory.create(**model_kwargs)

    if hasattr(model, "_client"):
        model_client = model._client
    else:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("Please install openai: pip install openai") from e
        api_base_url = getattr(model, "_url", getattr(config, "api_base_url", None))
        model_client = OpenAI(
            api_key=getattr(config, "api_key", None) or "dummy_key",
            base_url=api_base_url or "http://127.0.0.1:8000/v1",
        )

    model_name = getattr(config, "model", "")
    if hasattr(model, "model_type"):
        model_name = str(model.model_type)
        if hasattr(model.model_type, "value"):
            model_name = str(model.model_type.value)

    return _ModelFactorySummarizerProvider(
        config,
        model_client,
        str(model_name),
        system_message,
        accumulator=accumulator,
    )


# =============================================================================
# Agent
# =============================================================================


class PaperSummarizer:
    SUMMARIZER_SYSTEM_MSG = (
        "You are an expert technical editor for academic papers. "
        "Output only Markdown body text (no JSON, no preamble)."
    )

    def __init__(
        self,
        config: Union[SlidePlannerConfig, Any],
        summarizer_config: Optional[PaperSummarizerConfig] = None,
        accumulator: Optional[_TokenAccumulator] = None,
    ):
        self.config = config
        self.summarizer_config = summarizer_config or PaperSummarizerConfig()
        self._provider: Any = None
        self._accumulator = accumulator or _TokenAccumulator()

        out_base = Path(getattr(config, "output_dir", "outputs/section_slides_output"))
        self.output_dir = out_base / "paper_summarizer"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def provider(self):
        if self._provider is None:
            agent_config = getattr(self.config, "agent_config", None)
            if (
                isinstance(agent_config, dict)
                and agent_config.get("model_platform")
                and agent_config.get("model_type") is not None
            ):
                try:
                    self._provider = _create_provider_via_model_factory(
                        self.config,
                        self.SUMMARIZER_SYSTEM_MSG,
                        accumulator=self._accumulator,
                    )
                except Exception as e:
                    print(
                        f"Warning: PaperSummarizer ModelFactory setup failed ({e}); "
                        "falling back to providers.get_provider."
                    )
                    self._provider = get_provider(
                        self.config, self.SUMMARIZER_SYSTEM_MSG
                    )
            else:
                self._provider = get_provider(self.config, self.SUMMARIZER_SYSTEM_MSG)
        return self._provider

    def _summarize_one_block(
        self,
        section_markdown: str,
        chunk_index: int,
        continuation_instructions: str = "",
    ) -> Optional[str]:
        audience = getattr(self.config, "audience", "researchers")
        prompt = SUMMARIZER_PROMPT.format(
            continuation_instructions=continuation_instructions or "",
            section_markdown=section_markdown,
            compression_hint=self.summarizer_config.compression_hint,
            audience=audience,
        )
        if self.summarizer_config.save_intermediate and getattr(
            self.config, "save_intermediate", True
        ):
            p = self.output_dir / f"paper_summarizer_prompt_chunk_{chunk_index:04d}.txt"
            p.write_text(prompt, encoding="utf-8")

        raw = self.provider.generate(prompt)
        if raw is None:
            return None
        text = raw if isinstance(raw, str) else str(raw)
        text = _strip_markdown_fences(text)

        if self.summarizer_config.save_intermediate and getattr(
            self.config, "save_intermediate", True
        ):
            rpath = self.output_dir / f"paper_summarizer_raw_chunk_{chunk_index:04d}.txt"
            rpath.write_text(text, encoding="utf-8")
        return text.strip()

    def summarize(self, markdown: str) -> Tuple[Dict[str, Any], int, int]:
        if not self.summarizer_config.enabled:
            return (
                {
                    "success": True,
                    "summarized_markdown": markdown,
                    "error": None,
                    "summarized_path": None,
                    "_summarizer": {"skipped": True},
                },
                0,
                0,
            )

        md = markdown.strip()
        if not md:
            return (
                {
                    "success": False,
                    "summarized_markdown": markdown,
                    "error": "empty markdown",
                    "summarized_path": None,
                },
                0,
                0,
            )

        section_blocks = _split_paper_sections(md)
        if not section_blocks:
            section_blocks = [md]

        max_c = self.summarizer_config.max_chars_per_chunk
        call_idx = 0
        outs: List[str] = []

        for sec in section_blocks:
            subchunks = _split_oversized_block(sec, max_c)
            section_parts: List[str] = []
            for cont_instr, chunk in subchunks:
                part = self._summarize_one_block(
                    chunk, call_idx, continuation_instructions=cont_instr
                )
                call_idx += 1
                if not part:
                    return (
                        {
                            "success": False,
                            "summarized_markdown": markdown,
                            "error": f"LLM returned empty/None for chunk {call_idx - 1}",
                            "summarized_path": None,
                        },
                        self._accumulator.total_in,
                        self._accumulator.total_out,
                    )
                section_parts.append(part)
            outs.append("\n\n".join(section_parts))

        summarized = "\n\n".join(outs).strip() + ("\n" if outs else "")

        out_path = self.output_dir / "summarized_paper.md"
        if self.summarizer_config.save_intermediate and getattr(
            self.config, "save_intermediate", True
        ):
            out_path.write_text(summarized, encoding="utf-8")

        result = {
            "success": True,
            "summarized_markdown": summarized,
            "error": None,
            "summarized_path": str(out_path),
            "_summarizer": {
                "paper_sections": len(section_blocks),
                "llm_calls": call_idx,
            },
        }
        return result, self._accumulator.total_in, self._accumulator.total_out


def run_summarization_pipeline(
    markdown: str,
    config: Union[SlidePlannerConfig, Any],
    summarizer_config: Optional[PaperSummarizerConfig] = None,
) -> Tuple[str, int, int]:
    """
    Run summarization; on failure returns the original markdown.
    """
    agent = PaperSummarizer(config, summarizer_config)
    result, in_t, out_t = agent.summarize(markdown)
    if result.get("success") and result.get("summarized_markdown"):
        return str(result["summarized_markdown"]), in_t, out_t
    return markdown, in_t, out_t