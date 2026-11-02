import shutil
from pathlib import Path

import yaml

from tools.agent_config import validate_dir

AGENTS = Path(__file__).resolve().parents[1] / "agents"


def test_shipped_agent_configs_are_valid():
    results = validate_dir(AGENTS)
    assert set(results) == {"reviewer.yaml", "coach.yaml", "dojo.yaml"}
    assert all(errors == [] for errors in results.values()), results


def _copy_with(tmp_path, **overrides):
    shutil.copytree(AGENTS, tmp_path, dirs_exist_ok=True)
    config = yaml.safe_load((tmp_path / "reviewer.yaml").read_text())
    config.update(overrides)
    (tmp_path / "reviewer.yaml").write_text(yaml.safe_dump(config))
    return validate_dir(tmp_path)["reviewer.yaml"]


def test_agent_cannot_be_made_a_gate(tmp_path):
    errors = _copy_with(tmp_path, advisory=False)
    assert any(e.startswith("advisory:") for e in errors)


def test_unknown_tool_is_rejected(tmp_path):
    errors = _copy_with(tmp_path, tools=["git.push"])
    assert any(e.startswith("tools/0:") for e in errors)


def test_missing_prompt_file_is_reported(tmp_path):
    modes = [{"name": "fast", "stage": "commit", "prompt": "prompts/missing.md"}]
    errors = _copy_with(tmp_path, modes=modes)
    assert errors == ["modes/fast/prompt: prompts/missing.md not found"]
