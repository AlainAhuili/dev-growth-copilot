"""Load and validate agent definitions. Run as `python -m tools.agent_config agents`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

SCHEMA_FILE = "schema.json"


def validate_dir(agents_dir: Path) -> dict[str, list[str]]:
    """Return {config file name: [errors]} for every *.yaml in `agents_dir`."""
    schema = json.loads((agents_dir / SCHEMA_FILE).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    results: dict[str, list[str]] = {}
    for path in sorted(agents_dir.glob("*.yaml")):
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        errors = [
            f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in validator.iter_errors(config)
        ]
        if not errors:
            errors = _check_references(agents_dir, path, config)
        results[path.name] = errors
    return results


def _check_references(agents_dir: Path, path: Path, config: dict) -> list[str]:
    errors = []
    if config["name"] != path.stem:
        errors.append(f"name: '{config['name']}' does not match file name '{path.stem}'")
    for mode in config["modes"]:
        if not (agents_dir / mode["prompt"]).is_file():
            errors.append(f"modes/{mode['name']}/prompt: {mode['prompt']} not found")
    return errors


def main(argv: list[str]) -> int:
    agents_dir = Path(argv[0] if argv else "agents")
    results = validate_dir(agents_dir)
    if not results:
        print(f"no agent configs found in {agents_dir}")
        return 1
    failed = False
    for name, errors in results.items():
        print(f"{'FAIL' if errors else 'ok  '} {name}")
        for err in errors:
            print(f"     {err}")
        failed |= bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
