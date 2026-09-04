#!/usr/bin/env python3
"""
RST Integration Module
======================

Bridges RST (Rhetorical Structure Theory) parsing and slide grouping
with the slide-generation pipeline.

Exports used by the pipeline:
    create_config_from_agent_config
    run_rst_integration_pipeline
    run_commitment_building_pipeline
"""

import os
import sys
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

# Import ModelFactory for creating models
from camel.models import ModelFactory

# Import extract_text_from_responses for GPT-5 Responses API
from utils.llm.chat import extract_text_from_responses

from slide_generation.outline_generation import (
    SectionDiscourseParserConfig,
    SectionDiscourseParser,
    split_into_sections,
    md_to_rst_text,
    SlidePlannerConfig,
    SlidePlanner,
    SlideReviserConfig,
    SlideReviser,
    NarrativeCritic,
)
from slide_generation.outline_generation.slide_reviser import (
    _collect_paragraphs_from_rst_output,
)

# =============================================================================
# Helpers for model-specific API parameters
# =============================================================================

def _completion_token_kwargs(model_name: str, max_tokens: int) -> dict:
    """Return the correct token limit parameter for the model.
    GPT-5 requires max_completion_tokens instead of max_tokens."""
    if model_name and "gpt-5" in str(model_name).lower():
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _completion_kwargs(model_name: str, max_tokens: int, temperature: float) -> dict:
    """Return the correct kwargs for chat.completions.create().
    GPT-5 requires max_completion_tokens instead of max_tokens,
    and doesn't support custom temperature (only default 1)."""
    kwargs = _completion_token_kwargs(model_name, max_tokens)
    # GPT-5 doesn't support custom temperature, so omit it
    if not (model_name and "gpt-5" in str(model_name).lower()):
        kwargs["temperature"] = temperature
    return kwargs

# Token Accumulator
# =============================================================================

class _TokenAccumulator:
    """Accumulates input/output token counts across multiple LLM calls."""

    def __init__(self):
        self.total_in = 0
        self.total_out = 0

    def add(self, in_tok: int, out_tok: int):
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


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class RSTIntegrationConfig:
    """Configuration for RST integration in PosterAgent."""
    # Store agent_config for ModelFactory usage
    agent_config: Dict[str, Any] = field(default_factory=dict)
    
    # LLM settings - extracted from agent_config or defaults
    provider: str = "vllm"
    model: str = "openai/gpt-oss-20b"
    api_base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = "dummy_key"
    temperature: float = 0.1
    max_tokens: int = 4096
    
    # Output settings
    output_dir: str = "rst_outputs"
    save_intermediate: bool = True
    
    # Slide grouping settings
    presentation_length: int = 20  # Total presentation length in minutes
    audience: str = "researchers"
    
    # Processing settings
    group_subsections: bool = True
    use_references: bool = False


def create_config_from_agent_config(
    agent_config: Dict[str, Any],
    output_dir: str = "rst_outputs",
    presentation_length: int = 20,
    target_audience: str = "researchers",
) -> RSTIntegrationConfig:
    """
    Create RST integration config from PosterAgent's agent config.
    
    This function uses ModelFactory to create a model instance and extracts
    the necessary configuration for the RST parser modules.
    
    Args:
        agent_config: PosterAgent's agent configuration dictionary
        output_dir: Directory to save RST outputs
        presentation_length: Total presentation length in minutes
    
    Returns:
        RSTIntegrationConfig instance with model info extracted from ModelFactory
    """
    # Create a model instance using ModelFactory to get the actual configuration
    # This ensures we use the same model setup as the main pipeline
    try:
        # Create model using ModelFactory (same pattern as main pipeline)
        # Pass url if it exists in agent_config (for vLLM models)
        model_kwargs = {
            'model_platform': agent_config['model_platform'],
            'model_type': agent_config['model_type'],
            'model_config_dict': agent_config.get('model_config', {}),
        }
        
        # Add url parameter if present (needed for vLLM models)
        if 'url' in agent_config:
            model_kwargs['url'] = agent_config['url']
        
        test_model = ModelFactory.create(**model_kwargs)
        
        # Extract URL from the model instance
        api_base_url = getattr(test_model, '_url', None)
        if not api_base_url and 'url' in agent_config:
            api_base_url = agent_config['url']
        if not api_base_url:
            api_base_url = 'http://127.0.0.1:8000/v1'  # Default vLLM URL
        
        # Determine provider based on model platform
        model_platform_str = str(agent_config.get('model_platform', '')).lower()
        provider = "vllm"  # Default for local vLLM
        if "openai" in model_platform_str:
            provider = "openai"
        elif "anthropic" in model_platform_str:
            provider = "anthropic"
        elif "ollama" in model_platform_str:
            provider = "ollama"
        elif "deepinfra" in model_platform_str:
            provider = "openai"  # DeepInfra uses OpenAI-compatible API
        elif "openrouter" in model_platform_str:
            provider = "openai"  # OpenRouter uses OpenAI-compatible API
        elif "qwen" in model_platform_str:
            provider = "openai"  # Qwen uses OpenAI-compatible API
        
        # Get model name/type
        model = agent_config.get('model_type', 'openai/gpt-oss-20b')
        if hasattr(model, 'value'):
            model = model.value
        model = str(model)
        
    except Exception as e:
        print(f"Warning: Could not create model via ModelFactory: {e}")
        print("Falling back to direct config extraction")
        # Fallback to direct extraction
        api_base_url = agent_config.get('url', 'http://127.0.0.1:8000/v1')
        model_platform_str = str(agent_config.get('model_platform', '')).lower()
        provider = "vllm"
        if "openai" in model_platform_str:
            provider = "openai"
        elif "anthropic" in model_platform_str:
            provider = "anthropic"
        elif "deepinfra" in model_platform_str or "openrouter" in model_platform_str or "qwen" in model_platform_str:
            provider = "openai"  # These use OpenAI-compatible APIs
        
        model = agent_config.get('model_type', 'openai/gpt-oss-20b')
        if hasattr(model, 'value'):
            model = model.value
        model = str(model)
    
    return RSTIntegrationConfig(
        agent_config=agent_config,  # Store full agent_config for reference
        provider=provider,
        model=model,
        api_base_url=api_base_url,
        api_key=agent_config.get('api_key', 'dummy_key'),
        output_dir=output_dir,
        presentation_length=presentation_length,
        audience=target_audience,
    )


# =============================================================================
# RST Generation
# =============================================================================


def generate_rst_for_sections(
    markdown_content: str,
    config: RSTIntegrationConfig,
    poster_name: str = "paper",
    token_accumulators: Optional[Dict[str, _TokenAccumulator]] = None,
) -> Dict[str, Any]:
    """
    Generate RST discourse structure for each section of the document.
    
    Args:
        markdown_content: Raw markdown content from the paper
        config: RST integration configuration
        poster_name: Name identifier for the paper/poster
        token_accumulators: Dict mapping agent names to their accumulators.
        Expected keys: "summarizer", "discourse_parser".

        
    Returns:
        Dictionary with RST results for each section
    """
    print("\n" + "=" * 60)
    print("🌳 RUNNING DISCOURSE PARSER")
    print("=" * 60)
    
    # Create output directory
    rst_output_dir = Path(config.output_dir) / poster_name / "rst"
    rst_output_dir.mkdir(parents=True, exist_ok=True)
    
    # Create model using ModelFactory (same as main pipeline)
    print("Creating model using ModelFactory...")
    agent_config = config.agent_config
    model_kwargs = {
        'model_platform': agent_config['model_platform'],
        'model_type': agent_config['model_type'],
        'model_config_dict': agent_config.get('model_config', {}),
    }
    
    # Add url parameter if present (needed for vLLM models)
    if 'url' in agent_config:
        model_kwargs['url'] = agent_config['url']
    
    model = ModelFactory.create(**model_kwargs)
    
    # Extract client and model name from the model
    if hasattr(model, '_client'):
        model_client = model._client
    else:
        # Fallback: create OpenAI client from model's URL
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("Please install openai: pip install openai")
        api_base_url = getattr(model, '_url', config.api_base_url)
        model_client = OpenAI(
            api_key=config.api_key or "dummy_key",
            base_url=api_base_url
        )
    
    # Get model name from the model instance
    model_name = config.model
    if hasattr(model, 'model_type'):
        model_name = str(model.model_type)
        if hasattr(model.model_type, 'value'):
            model_name = str(model.model_type.value)
    
    _accumulators = token_accumulators or {}
    
    # Create a custom provider class that uses the ModelFactory model's client
    class ModelFactoryProvider:
        """Custom provider that uses ModelFactory-created model."""
        def __init__(self, rst_config, client, model_name, system_msg: str = "You are an expert RST discourse parser. Output only valid RS3 XML.", accumulator: Optional[_TokenAccumulator] = None):
            self.config = rst_config
            self.client = client
            self.model_name = model_name
            self.system_msg = system_msg
            self._accumulator = accumulator
        
        def generate(self, prompt: str) -> str:
            is_gpt5 = self.model_name and "gpt-5" in str(self.model_name).lower()

            # Use Responses API for GPT-5, Chat Completions API for others
            if is_gpt5:
                response = self.client.responses.create(
                    model=self.model_name,
                    input=prompt,
                    instructions=self.system_msg,
                    reasoning={"effort": "minimal"},
                    text={"verbosity": "low"},
                )
                text = extract_text_from_responses(response)
            else:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": self.system_msg},
                        {"role": "user", "content": prompt}
                    ],
                    **_completion_kwargs(self.model_name, self.config.max_tokens, self.config.temperature),
                )
                text = response.choices[0].message.content

            if self._accumulator is not None:
                in_t, out_t = _extract_tokens_from_response(response, is_gpt5)
                self._accumulator.add(in_t, out_t)
            return text


    
    # Create discourse parser config
    rst_config = SectionDiscourseParserConfig(
        provider=config.provider,
        model=config.model,
        api_key=config.api_key,
        api_base_url=config.api_base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        output_dir=str(rst_output_dir),
        save_intermediate=config.save_intermediate,
        use_references=config.use_references,
        group_subsections=config.group_subsections,
    )
    
    print(f"Provider: {rst_config.provider}")
    print(f"Model: {rst_config.model}")
    print(f"API Base URL: {config.api_base_url}")
    print(f"Output: {rst_config.output_dir}")
    print("-" * 60)
    
    # Create parser and inject our custom provider
    parser = SectionDiscourseParser(rst_config)
    

    # Inject the RST-parser provider
    custom_provider = ModelFactoryProvider(
        rst_config, model_client, model_name,
        accumulator=_accumulators.get("discourse_parser"),
    )

    parser._provider = custom_provider
    
    # Parse markdown into RST
    results = parser.parse_markdown(markdown_content)
    
    print("\n" + "-" * 60)
    success_count = sum(1 for r in results.values() if r.get("success"))
    print(f"Discourse parsing complete: {success_count}/{len(results)} sections successful")
    
    return results


# =============================================================================
# Slide Grouping with RST
# =============================================================================


def _truncate_for_terminal(text: str, max_chars: int) -> str:
    t = text.replace("\n", " ").strip()
    if len(t) <= max_chars:
        return t
    cut = t[: max_chars + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + " …"


def _format_merged_slides_for_user(
    results: Dict[str, Any],
    *,
    rst_output_dir: Optional[str] = None,
    max_slides_shown: int = 200,
    paragraph_snippet_chars: int = 240,
) -> str:
    """Human-readable summary of ``merged_slides`` for terminal review."""
    slides = results.get("merged_slides") or results.get("slides") or []
    para_map: Dict[str, str] = {}
    if rst_output_dir:
        try:
            para_map = _collect_paragraphs_from_rst_output(Path(rst_output_dir))
        except Exception as e:
            para_map = {}
            print(f"[format plan] Could not load paragraph text ({e}); showing IDs only.")

    lines: List[str] = [f"Slides: {len(slides)} total", ""]
    for i, s in enumerate(slides):
        if i >= max_slides_shown:
            lines.append(f"... ({len(slides) - max_slides_shown} more not listed)")
            break
        sn = s.get("slide_number", i + 1)
        sec = s.get("section", "")
        st = s.get("section_title", "")
        ttl = s.get("title", "")
        paras = s.get("paragraphs", [])
        head = f"  [{sn}] {ttl}"
        lines.append(head)
        idea = str(s.get("discussion_idea") or "").strip()
        if idea:
            lines.append(f"       discussion: {_truncate_for_terminal(idea, 500)}")
    return "\n".join(lines)


def get_user_feedback(
    results: Dict[str, Any],
    *,
    rst_output_dir: Optional[str] = None,
) -> tuple[Optional[Dict[str, Any]], bool]:
    """
    Show the current merged slide plan and collect one round of free-form user feedback.

    This function is intended to be called inside an already-interactive feedback
    flow. It does not check ``sys.stdin.isatty()`` and does not auto-accept based
    on environment settings.

    Pressing Enter or typing 'ok' / 'approve' accepts the plan.
    Any other input is treated as revision feedback and converted into the
    commentary JSON format expected by ``SlideReviser.revise_plan``.

    Args:
        results: Slide planner / reviser output containing ``merged_slides`` (or ``slides``).
        rst_output_dir: If set, paragraph IDs are expanded with short text snippets for review.

    Returns:
        ``(commentary_dict_or_none, revise_needed)``.
        When ``revise_needed`` is False, stop the revision loop.
        When True, pass ``commentary_dict`` to ``SlideReviser.revise_plan``.
    """
    print("\n" + "=" * 70)
    print("MERGED SLIDE PLAN REVIEW")
    print("=" * 70)
    print(_format_merged_slides_for_user(results, rst_output_dir=rst_output_dir))
    print("=" * 70)
    print("\nPress Enter or type 'ok' to approve.")
    print("Otherwise, enter feedback to revise the plan.")
    print("Example: split slide 3, improve flow between slides 5 and 6, shorten slide titles")

    feedback = input(
        "\nEnter feedback to revise the plan "
        "(or press Enter / type 'ok' to approve): "
    ).strip()

    try:
        from slide_generation.interaction_logger import log_user_feedback as _log_uf
        approved = feedback.lower() in ("", "ok", "approve", "approved", "done", "yes", "y")
        _log_uf(
            stage="outline_feedback_approval" if approved else "outline_feedback_input",
            content=feedback or "(empty)",
        )
    except Exception:
        pass

    if feedback.lower() in ("", "ok", "approve", "approved", "done", "yes", "y"):
        print("[get_user_feedback] Plan approved.")
        return None, False

    fixes = [line.strip() for line in feedback.splitlines() if line.strip()]
    commentary: Dict[str, Any] = {
        "overall_assessment": (
            "The user manually reviewed the merged slide plan and requested changes. "
            "Apply the feedback below while preserving all paragraph-ID constraints."
        ),
        "priority_fixes": fixes if fixes else [feedback],
        "notes": [feedback],
        "source": "user_feedback",
    }

    print(
        f"[get_user_feedback] Collected user feedback with "
        f"{len(commentary['priority_fixes'])} requested fix(es)."
    )
    return commentary, True


def plan_slides_with_rst(
    rst_output_dir: str,
    config: RSTIntegrationConfig,
    poster_name: str = "paper",
    commitment_md: Optional[str] = None,
    use_commitment_building: bool = False,
    target_audience: str = "researchers",
    outline_feedback: Optional[bool] = None,
    simulate_feedback_args: Optional[Any] = None,
    paper_summary: Optional[str] = None,
    token_accumulators: Optional[Dict[str, _TokenAccumulator]] = None,
) -> Dict[str, Any]:
    """
    Group paragraphs into slides using RST relations.
    
    Args:
        rst_output_dir: Path to RST parser output directory
        config: RST integration configuration
        poster_name: Name identifier for the paper/poster
        commitment_md: Optional global commitment markdown for the critic
        use_commitment_building: Whether critic prompts include commitment text
        target_audience: Audience string for planner / reviser
        interactive_user_feedback: If False, skip prompts and accept the plan.
            If None, prompt only when ``stdin`` is a TTY (batch jobs auto-accept).
        token_accumulators: Dict mapping agent names to their accumulators.
            Expected keys: "slide_planner", "narrative_critic", "slide_reviser".

    Returns:
        Dictionary with slide groupings
    """
    print("\n" + "=" * 60)
    print("📑 PLANNING SLIDES (RST-GUIDED)")
    print("=" * 60)
    
    # Create output directory
    slides_output_dir = Path(config.output_dir) / poster_name / "slides"
    slides_output_dir.mkdir(parents=True, exist_ok=True)
    
    # Create model using ModelFactory (same as main pipeline)
    print("Creating model using ModelFactory...")
    agent_config = config.agent_config
    model_kwargs = {
        'model_platform': agent_config['model_platform'],
        'model_type': agent_config['model_type'],
        'model_config_dict': agent_config.get('model_config', {}),
    }
    
    # Add url parameter if present (needed for vLLM models)
    if 'url' in agent_config:
        model_kwargs['url'] = agent_config['url']
    
    model = ModelFactory.create(**model_kwargs)
    
    # Extract client and model name from the model
    if hasattr(model, '_client'):
        model_client = model._client
    else:
        # Fallback: create OpenAI client from model's URL
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("Please install openai: pip install openai")
        api_base_url = getattr(model, '_url', config.api_base_url)
        model_client = OpenAI(
            api_key=config.api_key or "dummy_key",
            base_url=api_base_url
        )
    
    # Get model name from the model instance
    model_name = config.model
    if hasattr(model, 'model_type'):
        model_name = str(model.model_type)
        if hasattr(model.model_type, 'value'):
            model_name = str(model.model_type.value)

    _accumulators = token_accumulators or {}

    # Create a custom provider class that uses the ModelFactory model's client
    class ModelFactorySlideProvider:
        """Custom provider that uses ModelFactory-created model for slide planning."""
        def __init__(self, planner_config, client, model_name, accumulator: Optional[_TokenAccumulator] = None):
            self.config = planner_config
            self.client = client
            self.model_name = model_name
            self._accumulator = accumulator
        
        def generate(self, prompt: str) -> str:
            system_msg = "You are an expert at organizing academic content into presentation slides. Output only valid JSON."
            # Use Responses API for GPT-5, Chat Completions API for others
            is_gpt5 = self.model_name and "gpt-5" in str(self.model_name).lower()
            if is_gpt5:
                response = self.client.responses.create(
                    model=self.model_name,
                    input=prompt,
                    instructions=system_msg,
                    reasoning={"effort": "minimal"},
                    text={"verbosity": "low"},
                )
                text = extract_text_from_responses(response)
            else:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt}
                    ],
                    **_completion_kwargs(self.model_name, self.config.max_tokens, self.config.temperature),
                )
                text = response.choices[0].message.content
            if self._accumulator is not None:
                in_t, out_t = _extract_tokens_from_response(response, is_gpt5)
                self._accumulator.add(in_t, out_t)
            return text
    
    # Create slide planner config
    planner_config = SlidePlannerConfig(
        provider=config.provider,
        model=config.model,
        api_key=config.api_key,
        api_base_url=config.api_base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        output_dir=str(slides_output_dir),
        save_intermediate=config.save_intermediate,
        presentation_length=config.presentation_length,
        audience=target_audience,
    )
    
    print(f"Provider: {planner_config.provider}")
    print(f"Model: {planner_config.model}")
    print(f"API Base URL: {config.api_base_url}")
    print(f"Presentation length: {planner_config.presentation_length} minutes")
    print(f"Audience: {planner_config.audience}")
    print("-" * 60)
    
    # Create planner and inject our custom provider
    planner = SlidePlanner(planner_config)
    
    # Replace the provider to use our ModelFactory-based provider
    planner_provider = ModelFactorySlideProvider(
        planner_config, model_client, model_name,
        accumulator=_accumulators.get("planner"),
    )
    planner._provider = planner_provider
    
    # Plan slides
    results = planner.plan_from_rst_output(rst_output_dir)

    critic = NarrativeCritic(planner_config)
    critic.provider = ModelFactorySlideProvider(
        planner_config, model_client, model_name,
        accumulator=_accumulators.get("critic"),
    )

    reviser = SlideReviser(planner_config)
    reviser._provider = ModelFactorySlideProvider(
        planner_config, model_client, model_name,
        accumulator=_accumulators.get("reviser"),
    )

    critique_out = critic.critique_plan(
        results,
        rst_output_dir,
        commitment_md=commitment_md,
        use_commitment_building=use_commitment_building,
        round_number=0,
    )
    commentary = critique_out.get("commentary") if critique_out.get("success") else None

    results = reviser.revise_plan(
        results,
        rst_output_dir,
        commentary=commentary,
        judge_feedback=None,
        round_number=0,
    )

    if "summary" not in results:
        results["summary"] = results.get("revised_summary", results.get("summary", {}))
        results["merged_slides"] = results.get(
            "revised_merged_slides", results.get("merged_slides", [])
        )

    
    print("\n" + "-" * 60)
    print(f"Slide grouping complete: {results['summary']['total_slides']} slides created")
    
    return results


# =============================================================================
# Panel Conversion for PosterAgent Pipeline
# =============================================================================

def load_paragraph_content_from_rst(rst_output_dir: str) -> Dict[str, Dict[str, str]]:
    """
    Load paragraph content from RST output directory.
    
    Args:
        rst_output_dir: Path to RST output directory (e.g., rst_outputs/paper_name/rst)
        
    Returns:
        Dictionary mapping section_key -> {paragraph_name: paragraph_content}
    """
    paragraph_content = {}
    rst_path = Path(rst_output_dir)
    
    # Look for section directories
    if rst_path.exists():
        for section_dir in rst_path.iterdir():
            paragraphs_file = section_dir / "paragraphs.json"
            if paragraphs_file.exists():
                try:
                    paras = json.loads(paragraphs_file.read_text(encoding="utf-8"))
                    paragraph_content[section_dir.name] = paras
                except Exception as e:
                    print(f"Warning: Could not load {paragraphs_file}: {e}")
    
    return paragraph_content


def remove_references_and_below(markdown_content: str) -> str:
    """
    Remove references section and everything below it from markdown content.
    Looks for common section headers like References, Bibliography, Acknowledgments, Appendix, etc.
    """
    # Common section headers that typically come after the main content
    # Using case-insensitive matching
    reference_section_patterns = [
        re.compile(r'^#+\s*(?:References|Bibliography)', re.IGNORECASE),
        re.compile(r'^#+\s*(?:Acknowledgments?|Acknowledgements?)', re.IGNORECASE),
        re.compile(r'^#+\s*Appendix', re.IGNORECASE),
        re.compile(r'^#+\s*Author\s+Contributions', re.IGNORECASE),
        re.compile(r'^#+\s*Declaration\s+of\s+Competing\s+Interest', re.IGNORECASE),
        re.compile(r'^#+\s*Data\s+Availability', re.IGNORECASE),
        re.compile(r'^#+\s*Supplementary\s+Material', re.IGNORECASE),
    ]
    
    lines = markdown_content.split('\n')
    cutoff_index = len(lines)
    
    # Find the first occurrence of any reference section
    for i, line in enumerate(lines):
        for pattern in reference_section_patterns:
            if pattern.match(line):
                cutoff_index = i
                print(f"[Info] Found reference section at line {i+1}: {line.strip()}")
                print(f"[Info] Removing {len(lines) - cutoff_index} lines (references and below)")
                break
        if cutoff_index < len(lines):
            break
    
    # Return content up to (but not including) the reference section
    result = '\n'.join(lines[:cutoff_index])
    if cutoff_index < len(lines):
        print(f"[Info] Reduced content from {len(markdown_content)} to {len(result)} characters")
    return result

def run_rst_integration_pipeline(
    args,
    config: RSTIntegrationConfig,
    markdown_content: str,
    poster_name: str = "paper",
    commitment_md: Optional[str] = None,
    use_commitment_building: bool = False,
    target_audience: str = "researchers",
    outline_feedback: bool = False,
    simulate_feedback: bool = False,
) -> Tuple[List[Dict[str, str]], Dict[str, Tuple[int, int]]]:
    """
    Run the full RST integration pipeline and return simple JSON structure.
    
    Args:
        args: Pipeline arguments (paper_name, model_name_t, model_name_v)
        config: RST integration configuration
        markdown_content: Pre-extracted and cleaned markdown from the PDF
        poster_name: Name identifier for the paper/poster
        
    Returns:
        Tuple of (slides, token_usage) where token_usage maps agent name
        to (input_tokens, output_tokens).

    """
    print("\n" + "=" * 60)
    print("RUNNING RST INTEGRATION PIPELINE")
    print("=" * 60)

    start_time = time.time()

    # Per-agent token accumulators
    agent_names = ["discourse_parser", "planner", "critic", "reviser"]
    _accumulators = {name: _TokenAccumulator() for name in agent_names}

    # Strip references section to reduce token usage for RST parsing
    markdown_content = remove_references_and_below(markdown_content)

    pres_length = config.presentation_length
    poster_name_with_length = f"{poster_name}_{pres_length}min"
    
    # Save markdown for reference
    rst_dir = Path(config.output_dir) / poster_name_with_length
    rst_dir.mkdir(parents=True, exist_ok=True)
    (rst_dir / "source_markdown.md").write_text(markdown_content, encoding="utf-8")
    
    # Step 2: Generate RST for sections (RST parsing is same regardless of length)
    # Use base poster_name for RST to avoid duplicate parsing
    rst_base_dir = Path(config.output_dir) / poster_name / "rst"
    if not rst_base_dir.exists():
        rst_results = generate_rst_for_sections(
            markdown_content,
            config,
            poster_name,  # Use base name for RST parsing (can be reused)
            token_accumulators = _accumulators,
        )
    else:
        # Load existing RST results
        print(f"\n📂 Loading existing RST results from {rst_base_dir}")
        results_file = Path(config.output_dir) / poster_name / "rst" / "results.json"
        if results_file.exists():
            rst_results = json.loads(results_file.read_text(encoding="utf-8"))
        else:
            rst_results = generate_rst_for_sections(
                markdown_content,
                config,
                poster_name,
                token_accumulators = _accumulators,
            )
    
    # Step 3: Group slides using RST - THIS depends on presentation length
    # Use poster_name_with_length to get different groupings for different lengths
    rst_output_dir = str(Path(config.output_dir) / poster_name / "rst")
    
    # Override the output dir for slide grouping to include presentation length
    original_output_dir = config.output_dir
    config.output_dir = str(Path(original_output_dir) / poster_name_with_length)
    
    slide_groups = plan_slides_with_rst(
        rst_output_dir,
        config,
        poster_name_with_length,
        commitment_md=commitment_md,
        use_commitment_building=use_commitment_building,
        target_audience=target_audience,
        outline_feedback=outline_feedback,
        simulate_feedback_args=args if simulate_feedback else None,
        paper_summary=markdown_content if simulate_feedback else None,
        token_accumulators = _accumulators,
    )
    
    # Restore original output dir
    config.output_dir = original_output_dir
    
    # Step 4: Extract slides and create simple JSON structure
    merged_slides = slide_groups.get("merged_slides", [])
    
    # Load paragraph content from RST output directory
    paragraph_content = load_paragraph_content_from_rst(rst_output_dir)
    
    # Build simple JSON structure with title, content, and the planner's
    # discussion_idea (a short "what this slide is about" note). The nested
    # raw_content.json produced downstream via ``reformat_slides`` only copies
    # title+content, so discussion_idea lives only in raw_content_rst.json.
    result = []
    for slide in merged_slides:
        section_key = slide.get("section", "")
        slide_title = slide.get("title", "")
        slide_paragraphs = slide.get("paragraphs", [])
        slide_discussion = str(slide.get("discussion_idea") or "").strip()
        
        # Get the actual paragraph content for this slide
        section_paragraphs = paragraph_content.get(section_key, {})
        
        # Combine content from exactly the specified paragraphs
        slide_content_parts = []
        for para_name in slide_paragraphs:
            if para_name in section_paragraphs:
                slide_content_parts.append(section_paragraphs[para_name])
            else:
                # Try to find with different naming conventions
                for stored_name, content in section_paragraphs.items():
                    if para_name in stored_name or stored_name in para_name:
                        slide_content_parts.append(content)
                        break
        
        # Join paragraph content
        slide_content = "\n\n".join(slide_content_parts) if slide_content_parts else ""
        
        if(slide_content != ""):
            result.append({
                "title": slide_title,
                "content": slide_content,
                "discussion_idea": slide_discussion,
            })
        else:
            print(f"Warning: Empty slide content for {slide_title}")
    
            
    print(f"\n✅ Created {len(result)} slides with simple JSON structure")
    json.dump(result, open(f'contents/{args.paper_name}/<{args.model_name_t}_{args.model_name_v}>_raw_content_rst.json', 'w'), indent=4)

    token_usage = {name: (acc.total_in, acc.total_out) for name, acc in _accumulators.items()}
    return result, token_usage


def run_commitment_building_pipeline(
    markdown_content: str,
    config: RSTIntegrationConfig,
    poster_name: str = "paper",
    target_audience: str = "researchers",
    user_instructions: str = "",
) -> Tuple[str, int, int]:
    """
    Run the commitment building pipeline.

    Loads from cache if ``commitments.md`` already exists in the output
    directory; otherwise generates it via the LLM.

    Args:
        markdown_content: Pre-extracted and cleaned markdown from the PDF.
        config: RST integration configuration.
        poster_name: Name identifier for the paper/poster.
        target_audience: Audience description for the prompt.
        user_instructions: Free-form user instructions/preferences for the
            slide generation. Appended to the commitment prompt so the LLM
            incorporates them into the global contract.

    Returns:
        Tupple of (commitment_md, input_tokens, output_tokens).
    """
    commitment_output_dir = Path(config.output_dir) / poster_name / "commitment_output_dir"
    out_path = commitment_output_dir / "commitments.md"

    # ── Cache hit ────────────────────────────────────────────────────────
    if out_path.exists():
        commitment_md = out_path.read_text(encoding="utf-8").strip()
        if commitment_md:
            print(f"Loaded cached commitments.md from {out_path}")
            return commitment_md, 0, 0

    # ── Generate ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("RUNNING COMMITMENT BUILDING PIPELINE")
    print("=" * 60)

    commitment_output_dir.mkdir(parents=True, exist_ok=True)

    print("Creating model using ModelFactory...")
    agent_config = config.agent_config
    model_kwargs = {
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
        api_base_url = getattr(model, "_url", config.api_base_url)
        model_client = OpenAI(
            api_key=config.api_key or "dummy_key",
            base_url=api_base_url,
        )

    model_name = config.model
    if hasattr(model, "model_type"):
        mt = getattr(model, "model_type")
        model_name = str(getattr(mt, "value", mt))
    
    _accumulators = _TokenAccumulator()

    class ModelFactoryProvider:
        def __init__(self, rst_config, client, model_name: str):
            self.config = rst_config
            self.client = client
            self.model_name = model_name

        def generate(self, prompt: str) -> str:
            system_msg = (
                "You are CommitmentBuilder. "
                "Output ONLY the Markdown content for commitments.md. "
                "Do not wrap in code fences."
            )
            is_gpt5 = self.model_name and "gpt-5" in str(self.model_name).lower()
            if is_gpt5:
                response = self.client.responses.create(
                    model=self.model_name,
                    input=prompt,
                    instructions=system_msg,
                    reasoning={"effort": "minimal"},
                    text={"verbosity": "low"},
                )
                text = extract_text_from_responses(response) or ""
            else:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt},
                    ],
                    **_completion_kwargs(self.model_name, self.config.max_tokens, self.config.temperature),
                )
                text = response.choices[0].message.content or ""
            in_t, out_t = _extract_tokens_from_response(response, is_gpt5)
            _accumulators.add(in_t, out_t)
            return text

    commitment_building_config = SectionDiscourseParserConfig(
        provider=config.provider,
        model=model_name,
        api_key=config.api_key,
        api_base_url=config.api_base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        output_dir=str(commitment_output_dir),
        save_intermediate=config.save_intermediate,
        use_references=config.use_references,
        group_subsections=config.group_subsections,
        prompt_template_path="prompts/outline/commitment_builder_lite.txt",
        presentation_length=config.presentation_length,
    )

    print(f"Provider: {commitment_building_config.provider}")
    print(f"Model: {commitment_building_config.model}")
    print(f"API Base URL: {config.api_base_url}")
    print(f"Output: {commitment_building_config.output_dir}")
    print("-" * 60)

    provider = ModelFactoryProvider(commitment_building_config, model_client, model_name)

    template_path = Path(commitment_building_config.prompt_template_path)
    if not template_path.exists():
        raise FileNotFoundError(
            f"Commitment builder prompt template not found at: {template_path}. "
            "Expected it relative to your working directory."
        )
    prompt_template = template_path.read_text(encoding="utf-8").strip()

    print(f"Presentation length: {commitment_building_config.presentation_length}")
    full_prompt = (
        prompt_template
        + "\n\n"
        + "PAPER MARKDOWN (INPUT 1):\n"
        + markdown_content
        + "\n\n"
        + "TALK CONSTRAINTS (INPUT 2):\n"
        + f"- Presentation length (minutes): {commitment_building_config.presentation_length}\n"
        + f"- Target audience: '{target_audience}'\n"
    )
    if user_instructions:
        full_prompt += (
            "\nUSER INSTRUCTIONS (INPUT 3):\n"
            "The presenter has provided the following high-level instructions. "
            "These MUST be reflected in the commitment document — incorporate "
            "them into the relevant sections (talk contract, must-include/"
            "must-avoid, narrative spine, section plan priorities, etc.).\n"
            + user_instructions
            + "\n"
        )

    commitment_md = provider.generate(full_prompt).strip()

    if not commitment_md:
        raise RuntimeError("Model returned empty commitments.md content.")

    out_path.write_text(commitment_md + "\n", encoding="utf-8")
    print(f"Wrote commitments.md to: {out_path}")

    return commitment_md, _accumulators.total_in, _accumulators.total_out
