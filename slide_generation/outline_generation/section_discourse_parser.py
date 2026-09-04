#!/usr/bin/env python3
"""
Section-level Discourse Parser
==============================

This module generates RST discourse structure for each section of a markdown
document using an LLM to infer rhetorical relations between paragraphs within
each section.

Usage:
    python section_discourse_parser.py input.md --output-dir outputs/section_discourse_output
    python section_discourse_parser.py input.md --provider openai --model gpt-4o
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class SectionDiscourseParserConfig:
    """Configuration for section-level discourse parsing."""
    # LLM provider settings
    provider: Literal["openai", "anthropic", "vllm", "local"] = "openai"
    model: str = "gpt-4o"
    api_key: Optional[str] = None
    api_base_url: Optional[str] = None
    temperature: float = 0.1
    max_tokens: int = 4096
    
    # Output settings
    output_dir: str = "outputs/section_discourse_output"
    save_intermediate: bool = True
    
    # Section settings
    use_references: bool = False
    group_subsections: bool = True  # Group 3.1, 3.2 under section 3
    
    # Prompt settings
    prompt_template_path: str = "prompts/section_rst.txt"
    presentation_length: int = 15


# =============================================================================
# Text Processing (shared with other modules)
# =============================================================================

def md_to_rst_text(md: str) -> str:
    """Clean markdown for RST parsing."""
    md = re.sub(r"```[\s\S]*?```", "", md)
    md = re.sub(r"`([^`]*)`", r"\1", md)
    md = re.sub(r"!\[.*?\]\(.*?\)", "", md)
    md = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", md)
    md = re.sub(r"^[\-\*\+]\s+", "", md, flags=re.MULTILINE)
    md = re.sub(r"\*\*(.*?)\*\*", r"\1", md)
    md = re.sub(r"\*(.*?)\*", r"\1", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def slugify_section(title: str) -> str:
    """Convert heading to slug."""
    s = title.strip().lower()
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "section"


def extract_major_section_number(title: str) -> Optional[str]:
    """Extract major section number from a heading (e.g., '3.1 Task' -> '3')."""
    match = re.match(r"^(\d+)(?:\.\d+)*\.?\s+", title)
    if match:
        return match.group(1)
    return None


def split_into_sections(
    raw_md: str,
    use_references: bool = False,
    group_subsections: bool = True
) -> Dict[str, Dict[str, Any]]:
    """
    Split markdown into sections, optionally grouping subsections.
    
    Returns:
        Dictionary mapping section_slug to:
        - title: Section title
        - raw_text: Raw text content
        - paragraphs: List of paragraph texts
        - paragraph_names: List of paragraph names
        - subsections: List of subsection titles
    """
    if not use_references:
        m = re.search(r"^\s*#{1,6}\s+references\s*$", raw_md,
                      flags=re.IGNORECASE | re.MULTILINE)
        if m:
            raw_md = raw_md[:m.start()]
    
    md = md_to_rst_text(raw_md)
    lines = md.splitlines()
    heading_re = re.compile(r"^\s*##\s+(.*?)\s*$")
    
    if not group_subsections:
        # Each heading is its own section
        sections = {}
        current_section = "root"
        current_title = "Root"
        section_lines = []
        
        def flush_section():
            nonlocal section_lines, current_section, current_title
            if not section_lines:
                return
            
            raw_text = "\n".join(section_lines).strip()
            if not raw_text:
                section_lines = []
                return
            
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", raw_text) if p.strip()]
            paragraphs = [p for p in paragraphs if not re.match(r"^\s*#{1,6}\s+", p)]
            
            if not paragraphs:
                section_lines = []
                return
            
            paragraph_names = [f"{current_section}_{i}" for i in range(len(paragraphs))]
            
            sections[current_section] = {
                "title": current_title,
                "raw_text": raw_text,
                "paragraphs": paragraphs,
                "paragraph_names": paragraph_names,
                "subsections": [current_title],
            }
            section_lines = []
        
        for line in lines:
            h = heading_re.match(line)
            if h:
                flush_section()
                current_title = h.group(1).strip()
                current_section = slugify_section(current_title)
                continue
            section_lines.append(line)
        
        flush_section()
        return sections
    
    # Group subsections under parent sections
    raw_sections = []
    current_title = "Root"
    current_lines = []
    
    for line in lines:
        h = heading_re.match(line)
        if h:
            if current_lines:
                raw_sections.append((current_title, current_lines))
            current_title = h.group(1).strip()
            current_lines = []
            continue
        current_lines.append(line)
    
    if current_lines:
        raw_sections.append((current_title, current_lines))
    
    # Group by major section number
    grouped = {}
    
    for title, lines in raw_sections:
        major_num = extract_major_section_number(title)
        
        if major_num:
            key = f"section_{major_num}"
        else:
            key = slugify_section(title)
        
        if key not in grouped:
            grouped[key] = {"titles": [], "lines": [], "subsections": []}
        
        grouped[key]["titles"].append(title)
        grouped[key]["subsections"].append(title)
        grouped[key]["lines"].extend(lines)
    
    # Build final sections dict
    sections = {}
    
    for key, data in grouped.items():
        raw_text = "\n".join(data["lines"]).strip()
        if not raw_text:
            continue
        
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", raw_text) if p.strip()]
        paragraphs = [p for p in paragraphs if not re.match(r"^\s*#{1,6}\s+", p)]
        
        if not paragraphs:
            continue
        
        title = data["titles"][0]
        paragraph_names = [f"{key}_{i}" for i in range(len(paragraphs))]
        
        sections[key] = {
            "title": title,
            "raw_text": raw_text,
            "paragraphs": paragraphs,
            "paragraph_names": paragraph_names,
            "subsections": data["subsections"],
        }
    
    return sections


from .providers import LLMProvider, get_provider, load_prompt


# =============================================================================
# Prompt Building
# =============================================================================

def get_section_prompt_template() -> str:
    """Return the default section RST prompt template that outputs a binary-tree JSON IR."""
    return load_prompt("section_rst.txt")

def format_paragraph_input(paragraphs: List[str], names: List[str]) -> str:
    """Format paragraphs for the prompt."""
    lines = []
    for name, text in zip(names, paragraphs):
        truncated = text
        truncated = truncated.replace("\n", " ")
        lines.append(f"[{name}]\n{truncated}\n")
    return "\n".join(lines)

def save_paragraphs_json(path: Path, paragraphs_dict: Dict[str, str]) -> None:
    """
    Save a name -> paragraph text mapping to paragraphs.json.
    Does not perform any RST generation; only writes the JSON file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(paragraphs_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_paragraphs_json_per_section(
    raw_md: str,
    output_dir: str | Path,
    use_references: bool = False,
    group_subsections: bool = True,
) -> Dict[str, str]:
    """
    Split markdown into sections (same logic as parse_markdown) and save
    paragraphs.json for each section under output_dir / section_key /.
    No RST or LLM; only section splitting and file writing.

    Returns:
        Mapping of section_key -> absolute path of saved paragraphs.json.
    """
    output_dir = Path(output_dir)
    sections = split_into_sections(raw_md, use_references, group_subsections)
    saved = {}
    for section_key, section_data in sections.items():
        section_dir = output_dir / section_key
        path = section_dir / "paragraphs.json"
        paragraphs_dict = dict(
            zip(
                section_data["paragraph_names"],
                section_data["paragraphs"],
            )
        )
        save_paragraphs_json(path, paragraphs_dict)
        saved[section_key] = str(path.resolve())
    return saved

def build_section_prompt(
    section_title: str,
    subsections: List[str],
    paragraphs: List[str],
    paragraph_names: List[str],
    prompt_template: Optional[str] = None
) -> str:
    """Build the prompt for a single section."""
    if prompt_template is None:
        prompt_template = get_section_prompt_template()
    
    paragraph_input = format_paragraph_input(paragraphs, paragraph_names)
    subsections_str = ", ".join(subsections) if subsections else section_title
    
    return prompt_template.format(
        section_title=section_title,
        subsections=subsections_str,
        paragraph_input=paragraph_input
    )

def extract_json_tree_from_response(response: str) -> Dict[str, Any]:
    """
    Extract the JSON discourse tree object from LLM response.
    Supports:
      - raw JSON
      - fenced code blocks ```json ... ```
      - extra chatter before/after JSON
    """
    # 1) Prefer fenced code blocks
    code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
    if code_block_match:
        candidate = code_block_match.group(1).strip()
        return json.loads(candidate)

    # 2) Otherwise, try to locate the first JSON object by brace matching
    start = response.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")

    # Brace matching to find a full top-level JSON object
    depth = 0
    end = None
    for i in range(start, len(response)):
        ch = response[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end is None:
        raise ValueError("Unbalanced braces; could not find end of JSON object")

    candidate = response[start:end].strip()
    return json.loads(candidate)

def validate_json_rst_tree(tree: Dict[str, Any], edu_ids: Optional[Set[str]] = None) -> Dict[str, Any]:
    errors: List[str] = []

    if not isinstance(tree, dict):
        return {"ok": False, "errors": ["Top-level output must be a JSON object (dict)."]}

    root_id = tree.get("root")
    groups = tree.get("groups")

    if not isinstance(root_id, str) or not root_id:
        errors.append('Missing or invalid "root" (must be a non-empty string).')

    if not isinstance(groups, dict) or not groups:
        errors.append('Missing or invalid "groups" (must be a non-empty object).')
        return {"ok": False, "errors": errors}

    if root_id not in groups:
        errors.append(f'root "{root_id}" not found in groups.')

    # Validate each group schema
    for gid, g in groups.items():
        if not isinstance(g, dict):
            errors.append(f'Group "{gid}" must be an object.')
            continue
        gtype = g.get("type")
        rel = g.get("relation")

        if not isinstance(rel, str) or not rel:
            errors.append(f'Group "{gid}" missing/invalid "relation" string.')

        if gtype == "rst":
            if "nucleus" not in g or "satellite" not in g:
                errors.append(f'Group "{gid}" type="rst" must have "nucleus" and "satellite".')
        if gtype == "multinuc":
            if "left" not in g or "right" not in g:
                errors.append(f'Group "{gid}" type="multinuc" must have "left" and "right".')

    # Helper to list children and referenced nodes
    def get_children(gid: str) -> List[str]:
        g = groups[gid]
        if g.get("type") == "rst":
            return [g.get("nucleus"), g.get("satellite")]
        return [g.get("left"), g.get("right")]

    # Check references + collect leaves
    visited: Set[str] = set()
    stack: Set[str] = set()
    leaves: List[str] = []

    def dfs(node: str):
        if node in stack:
            errors.append(f"Cycle detected involving node '{node}'.")
            return
        if node in visited:
            return
        visited.add(node)

        # If node is a group, traverse children
        if node in groups:
            stack.add(node)
            for ch in get_children(node):
                if not isinstance(ch, str) or not ch:
                    errors.append(f'Group "{node}" has missing/invalid child id.')
                    continue
                if ch in groups:
                    dfs(ch)
                else:
                    # Leaf EDU
                    leaves.append(ch)
                    if edu_ids is not None and ch not in edu_ids:
                        errors.append(f'Leaf EDU "{ch}" not in provided edu_ids inventory.')
            stack.remove(node)
        else:
            # Root must be a group; non-group here is suspicious
            errors.append(f'Node "{node}" referenced as group but not found in groups.')

    if isinstance(root_id, str) and root_id in groups:
        dfs(root_id)

    # Optional: ensure every EDU is used exactly once
    if edu_ids is not None:
        leaf_counts: Dict[str, int] = {}
        for l in leaves:
            leaf_counts[l] = leaf_counts.get(l, 0) + 1

        missing = sorted(list(edu_ids - set(leaves)))
        extra = sorted(list(set(leaves) - edu_ids))
        dup = sorted([k for k, v in leaf_counts.items() if v > 1])

    return {"ok": len(errors) == 0, "errors": errors}

def extract_relations_from_json_tree(tree: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract attachment relations from JSON tree.
    Output edges are explicit and unambiguous.
    """
    groups: Dict[str, Any] = tree.get("groups", {})
    root_id: str = tree.get("root", "")

    relations: List[Dict[str, Any]] = []

    # Emit group metadata rows (optional, but handy)
    for gid, g in groups.items():
        relations.append({
            "type": "group_definition",
            "group": gid,
            "group_type": g.get("type"),
            "group_relation": g.get("relation"),
            "is_root": (gid == root_id),
        })

    # Emit child -> group attachments
    for gid, g in groups.items():
        gtype = g.get("type")
        grel = g.get("relation")

        if gtype == "rst":
            nuc = g.get("nucleus")
            sat = g.get("satellite")

            relations.append({
                "type": "child_to_group",
                "from": nuc,
                "to": gid,
                "role": "nucleus",
                "group_relation": grel,
            })
            relations.append({
                "type": "child_to_group",
                "from": sat,
                "to": gid,
                "role": "satellite",
                "group_relation": grel,
            })

        elif gtype == "multinuc":
            left = g.get("left")
            right = g.get("right")

            relations.append({
                "type": "child_to_group",
                "from": left,
                "to": gid,
                "role": "left",
                "group_relation": grel,
            })
            relations.append({
                "type": "child_to_group",
                "from": right,
                "to": gid,
                "role": "right",
                "group_relation": grel,
            })

    return relations



# =============================================================================
# Main Parser Class
# =============================================================================

class SectionDiscourseParser:
    """
    Section-level Discourse Parser.
    
    Generates RST discourse structure for each section using an LLM.
    """
    
    def __init__(self, config: Optional[SectionDiscourseParserConfig] = None):
        self.config = config or SectionDiscourseParserConfig()
        self.output_dir = Path(self.config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Load custom prompt template if provided
        self.prompt_template = None
        if self.config.prompt_template_path:
            prompt_path = Path(self.config.prompt_template_path)
            if not prompt_path.is_absolute():
                project_root = Path(__file__).parent
                prompt_path = project_root / prompt_path
            
            if prompt_path.exists():
                self.prompt_template = prompt_path.read_text(encoding="utf-8")
        
        self._provider = None
    
    RST_SYSTEM_MSG = "You are an expert RST discourse parser. Output only valid RS3 XML."

    @property
    def provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = get_provider(self.config, self.RST_SYSTEM_MSG)
        return self._provider
    
    def parse_section(
    self,
    section_key: str,
    section_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Parse a single section using LLM (JSON binary-tree discourse IR).

        Returns:
            Dictionary with json_tree_content, relations, validation info
        """
        title = section_data["title"]
        subsections = section_data.get("subsections", [title])
        paragraphs = section_data["paragraphs"]
        paragraph_names = section_data["paragraph_names"]

        if len(paragraphs) < 2:
            return {
                "success": False,
                "error": f"Section has only {len(paragraphs)} paragraph(s), need at least 2",
                "paragraphs": len(paragraphs),
            }

        # Build prompt
        prompt = build_section_prompt(
            title,
            subsections,
            paragraphs,
            paragraph_names,
            self.prompt_template
        )

        # Create section output directory
        section_dir = self.output_dir / section_key
        section_dir.mkdir(parents=True, exist_ok=True)

        if self.config.save_intermediate:
            (section_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
            (section_dir / "paragraphs.json").write_text(
                json.dumps(dict(zip(paragraph_names, paragraphs)), ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

        # Call LLM
        try:
            print(f"Discourse parsing for section {section_key}")
            response = self.provider.generate(prompt)

            if self.config.save_intermediate:
                (section_dir / "llm_response.txt").write_text(response, encoding="utf-8")

            tree_obj = extract_json_tree_from_response(response)

            # Validate against paragraph_names (EDU inventory)
            validation = validate_json_rst_tree(tree_obj, edu_ids=set(paragraph_names))

            if not validation["ok"]:
                # Still save what we got for debugging
                tree_path = section_dir / "section_tree.json"
                tree_path.write_text(json.dumps(tree_obj, ensure_ascii=False, indent=2), encoding="utf-8")

                return {
                    "success": False,
                    "error": "Invalid JSON RST tree: " + "; ".join(validation["errors"]),
                    "tree_path": str(tree_path),
                    "tree_content": tree_obj,
                    "validation": validation,
                    "paragraphs": len(paragraphs),
                    "subsections": subsections,
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "paragraphs": len(paragraphs),
            }

        # Save JSON tree
        tree_path = section_dir / "section_tree.json"
        tree_path.write_text(json.dumps(tree_obj, ensure_ascii=False, indent=2), encoding="utf-8")


        return {
            "success": True,
            "tree_path": str(tree_path),
            "tree_content": tree_obj,
            "validation": validation,
            "paragraphs": len(paragraphs),
            "subsections": subsections,
        }
    
    def parse_markdown(self, raw_md: str) -> Dict[str, Any]:
        """
        Parse markdown into section-level RST trees.
        
        Args:
            raw_md: Raw markdown content
            
        Returns:
            Dictionary with results for each section
        """
        # Split into sections
        sections = split_into_sections(
            raw_md,
            self.config.use_references,
            self.config.group_subsections
        )
        
        print(f"Found {len(sections)} sections")
        
        results = {}
        
        for section_key, section_data in sections.items():
            title = section_data["title"]
            num_paragraphs = len(section_data["paragraphs"])
            
            print(f"\nProcessing section: {section_key} ({title})")
            print(f"  Paragraphs: {num_paragraphs}")
            if len(section_data.get("subsections", [])) > 1:
                print(f"  Subsections: {', '.join(section_data['subsections'])}")
            
            if num_paragraphs < 2:
                print(f"  Skipping: need at least 2 paragraphs for RST")
                results[section_key] = {
                    "title": title,
                    "success": False,
                    "error": f"Only {num_paragraphs} paragraph(s), need at least 2",
                    "paragraphs": num_paragraphs,
                }
                continue
            
            print(f"  Calling LLM...")
            result = self.parse_section(section_key, section_data)
            result["title"] = title
            results[section_key] = result
            
            if result["success"]:
                print(f"  Success: {len(result['tree_content']['groups'])} groups extracted")
            else:
                print(f"  Failed: {result.get('error', 'Unknown error')}")
        
        # Save overall results
        results_path = self.output_dir / "results.json"
        results_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        
        return results
    
    def parse_file(self, input_path: str | Path) -> Dict[str, Any]:
        """Parse a markdown file."""
        input_path = Path(input_path)
        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")
        
        raw_md = input_path.read_text(encoding="utf-8")
        return self.parse_markdown(raw_md)
