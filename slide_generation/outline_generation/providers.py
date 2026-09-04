"""
Shared LLM provider infrastructure for the outline_generation package.

Every agent in the package (discourse parser, slide planner, narrative critic,
slide reviser) uses the same provider classes.  The only
per-agent difference is the ``system_message`` passed at construction time.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts" / "outline"


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class LLMProvider:
    """Base class for LLM providers."""

    def __init__(self, config, system_message: str = ""):
        self.config = config
        self.system_message = system_message

    def generate(self, prompt: str) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Concrete providers
# ---------------------------------------------------------------------------

class OpenAIProvider(LLMProvider):
    def __init__(self, config, system_message: str = ""):
        super().__init__(config, system_message)
        from openai import OpenAI

        api_key = config.api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OpenAI API key not found")
        self.client = OpenAI(api_key=api_key)

    def generate(self, prompt: str) -> str:
        messages = []
        if self.system_message:
            messages.append({"role": "system", "content": self.system_message})
        messages.append({"role": "user", "content": prompt})
        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        return response.choices[0].message.content


class AnthropicProvider(LLMProvider):
    def __init__(self, config, system_message: str = ""):
        super().__init__(config, system_message)
        import anthropic

        api_key = config.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("Anthropic API key not found")
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate(self, prompt: str) -> str:
        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.config.temperature,
        }
        if self.system_message:
            kwargs["system"] = self.system_message
        message = self.client.messages.create(**kwargs)
        return message.content[0].text


class VLLMProvider(LLMProvider):
    def __init__(self, config, system_message: str = ""):
        super().__init__(config, system_message)
        from openai import OpenAI

        base_url = config.api_base_url or "http://127.0.0.1:8000/v1"
        self.client = OpenAI(
            api_key=config.api_key or "dummy_key",
            base_url=base_url,
        )

    def generate(self, prompt: str) -> str:
        messages = []
        if self.system_message:
            messages.append({"role": "system", "content": self.system_message})
        messages.append({"role": "user", "content": prompt})
        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "vllm": VLLMProvider,
}


def get_provider(config, system_message: str = "") -> LLMProvider:
    """Create an LLM provider from *config*.provider with the given system message."""
    cls = _PROVIDERS.get(config.provider)
    if cls is None:
        raise ValueError(f"Unknown provider: {config.provider}")
    return cls(config, system_message)


# ---------------------------------------------------------------------------
# JSON extraction helper (used by planner, reviser, critic)
# ---------------------------------------------------------------------------

def load_prompt(name: str) -> str:
    """Load a prompt template from ``prompts/outline/<name>``."""
    path = _PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def extract_json_from_response(response: str) -> Dict[str, Any]:
    """Extract the first JSON object from an LLM response string."""
    # Prefer fenced code blocks
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
    if m:
        return json.loads(m.group(1).strip())
    # Fall back to raw JSON object
    m = re.search(r"\{[\s\S]*\}", response)
    if m:
        return json.loads(m.group(0))
    raise ValueError("No JSON found in response")
