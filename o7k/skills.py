"""Skill loader and runner.

Loads a skill's SKILL.md (frontmatter + prompt body), runs its pre-context
scripts to gather context blocks, assembles the full prompt, calls the LLM
via o7k.llm, and returns the raw response text.

The caller (the manager) is responsible for parsing the response according
to the skill's output_contract.
"""

from __future__ import annotations

import os
import subprocess
import re
from pathlib import Path

import yaml

from o7k import llm

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_DIR = _REPO_ROOT / "skills"


# ---------------------------------------------------------------------------
# SKILL.md parsing
# ---------------------------------------------------------------------------

def _parse_skill_md(path: Path) -> tuple[dict, str]:
    """Parse a SKILL.md file into (frontmatter_dict, prompt_body)."""
    text = path.read_text()

    # Split YAML frontmatter from markdown body
    # Format: --- \n yaml \n --- \n body
    m = re.match(r"^---\s*\n(.*?\n)---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        raise ValueError(f"SKILL.md missing YAML frontmatter: {path}")

    frontmatter = yaml.safe_load(m.group(1))
    body = m.group(2).strip()
    return frontmatter, body


def load(skill_name: str) -> tuple[dict, str]:
    """Load a skill by name. Returns (frontmatter, prompt_body)."""
    skill_dir = _SKILLS_DIR / skill_name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Skill not found: {skill_md}")
    return _parse_skill_md(skill_md)


# ---------------------------------------------------------------------------
# Pre-context script execution
# ---------------------------------------------------------------------------

def _run_pre_context_scripts(
    skill_dir: Path,
    scripts: list[str],
    env: dict[str, str],
    timeout: int = 3600,  # pre-context scripts may run full builds
) -> dict[str, str]:
    """Run each pre-context script and capture its output blocks.

    Each script prints lines like:
        === block_name ===
        key: value
        ...

    Returns {block_name: full_text_including_header}.
    """
    full_env = {**os.environ, **env}
    blocks: dict[str, str] = {}

    for script_name in scripts:
        script_path = skill_dir / script_name
        if not script_path.exists():
            blocks[script_name] = f"=== {script_name} ===\nerror: script not found\n"
            continue

        result = subprocess.run(
            ["python3", str(script_path)],
            capture_output=True,
            text=True,
            env=full_env,
            timeout=timeout,
            cwd=skill_dir,
        )

        output = result.stdout
        # Parse block names from output
        current_block = None
        current_lines: list[str] = []

        for line in output.splitlines():
            header_match = re.match(r"^===\s+(.+?)\s+===$", line)
            if header_match:
                if current_block is not None:
                    blocks[current_block] = "\n".join(current_lines)
                current_block = header_match.group(1)
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_block is not None:
            blocks[current_block] = "\n".join(current_lines)

        if result.stderr:
            print(f"[skills] {script_name} stderr: {result.stderr.strip()}")

    return blocks


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _assemble_prompt(
    prompt_body: str,
    context_blocks: dict[str, str],
    env: dict[str, str],
) -> str:
    """Build the full prompt: context blocks + skill prompt body."""
    parts: list[str] = []

    # Package identity block (always included)
    parts.append("=== package_identity ===")
    parts.append(f"PACKAGE: {env.get('PACKAGE', 'unknown')}")
    parts.append(f"OPENSTACK_SERIES: {env.get('OPENSTACK_SERIES', 'unknown')}")
    parts.append(f"UBUNTU_SERIES: {env.get('UBUNTU_SERIES', 'unknown')}")
    parts.append("")

    # Context blocks from pre-context scripts
    for _name, block_text in context_blocks.items():
        parts.append(block_text)
        parts.append("")

    # The skill prompt itself
    parts.append("--- INSTRUCTIONS ---")
    parts.append(prompt_body)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(skill_name: str, env: dict[str, str]) -> str:
    """Load a skill, run its pre-context scripts, call the LLM, return response.

    *env* is a dict of environment variables passed to pre-context scripts
    and used to build the package_identity context block.  Expected keys:
    PACKAGE, OPENSTACK_SERIES, UBUNTU_SERIES.

    Returns the raw LLM response text.
    """
    skill_dir = _SKILLS_DIR / skill_name
    frontmatter, prompt_body = load(skill_name)

    # Run pre-context scripts
    scripts = frontmatter.get("pre_context_scripts") or []
    timeout = int(frontmatter.get("pre_context_timeout_seconds", 30))
    context_blocks = _run_pre_context_scripts(skill_dir, scripts, env, timeout=timeout)

    # Assemble prompt
    prompt = _assemble_prompt(prompt_body, context_blocks, env)

    # Call LLM
    model_tier = frontmatter.get("model_tier", "cheap")
    print(f"[skills] calling LLM ({model_tier}) for skill={skill_name}")
    response = llm.call(prompt, model_tier=model_tier)

    return response
