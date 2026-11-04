"""The Coach's model calls: no tools, no settings files, budget cap, structured output."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"


def load_mode(mode_name: str) -> dict[str, Any]:
    config = yaml.safe_load((AGENTS_DIR / "coach.yaml").read_text(encoding="utf-8"))
    mode = next(m for m in config["modes"] if m["name"] == mode_name)
    return {
        "version": config["version"],
        "model": mode.get("model", config["model"]),
        "prompt": (AGENTS_DIR / mode["prompt"]).read_text(encoding="utf-8"),
    }


async def _ask(system_prompt: str, prompt: str, schema: dict, model: str) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        tools=[],
        allowed_tools=[],
        setting_sources=[],
        max_turns=3,
        max_budget_usd=0.25,
        output_format={"type": "json_schema", "schema": schema},
    )
    result = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result is None or result.is_error or not isinstance(result.structured_output, dict):
        raise RuntimeError(f"coach failed: {getattr(result, 'errors', None)}")
    return result.structured_output


def ask_json(mode_name: str, prompt: str, schema: dict) -> dict:
    mode = load_mode(mode_name)
    return asyncio.run(_ask(mode["prompt"], prompt, schema, mode["model"]))
