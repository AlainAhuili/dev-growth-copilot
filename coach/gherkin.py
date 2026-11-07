"""Deterministic part of spec mode: read a Gherkin feature, flag what is provably wrong with
it, and match it against pytest-bdd results. The model only asks questions on top of this."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

STEP_KEYWORDS = ("Given", "When", "Then", "And", "But", "*")
SCENARIO_KEYWORDS = ("Scenario Outline", "Scenario Template", "Scenario", "Example")
# Words that make a Then step impossible to check: what exactly is "correctly"?
VAGUE = re.compile(
    r"\b(correctly|properly|as expected|appropriate(?:ly)?|reasonabl[ey]|quickly|fast|"
    r"user[- ]friendly|works?|etc|and so on|some|several|many)\b",
    re.I)
PLACEHOLDER = re.compile(r"<([^<>]+)>")


@dataclass
class Step:
    keyword: str  # effective keyword: And/But/* take the one before them
    text: str
    line: int


@dataclass
class Scenario:
    name: str
    line: int
    outline: bool = False
    steps: list[Step] = field(default_factory=list)
    examples_header: list[str] | None = None
    examples_rows: int = 0
    tags: list[str] = field(default_factory=list)


@dataclass
class Feature:
    name: str = ""
    background: list[Step] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)


def _keyword(line: str, keywords: tuple[str, ...]) -> tuple[str, str] | None:
    for kw in keywords:
        if line.startswith(kw + ":"):
            return kw, line[len(kw) + 1:].strip()
        if kw in STEP_KEYWORDS and (line == kw or line.startswith(kw + " ")):
            return kw, line[len(kw):].strip()
    return None


def parse(text: str) -> Feature:
    """A small, forgiving Gherkin reader: Feature, Rule, Background, Scenario (Outline),
    steps, Examples tables, tags. Doc strings and step tables are skipped, not interpreted."""
    feature = Feature()
    current: Scenario | None = None
    in_background = in_examples = in_docstring = False
    previous_kw = "Given"
    tags: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith(('"""', "```")):
            in_docstring = not in_docstring
            continue
        if in_docstring or not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            tags += line.split()
            continue
        if line.startswith("Feature:"):
            feature.name, tags = line[8:].strip(), []  # feature tags aren't a scenario's
            continue
        if line.startswith("Rule:"):
            current, in_background, in_examples = None, False, False
            continue
        if line.startswith("Background:"):
            current, in_background, in_examples, previous_kw = None, True, False, "Given"
            continue
        if (sc := _keyword(line, SCENARIO_KEYWORDS)) is not None:
            kw, name = sc
            current = Scenario(name=name, line=n, outline=kw != "Scenario" and kw != "Example",
                               tags=tags)
            feature.scenarios.append(current)
            tags, in_background, in_examples, previous_kw = [], False, False, "Given"
            continue
        if line.startswith(("Examples:", "Scenarios:")):
            in_examples = True
            continue
        if line.startswith("|"):
            if in_examples and current is not None:
                cells = [c.strip() for c in line.strip("|").split("|")]
                if current.examples_header is None:
                    current.examples_header = cells
                elif cells != current.examples_header:  # a second Examples block repeats it
                    current.examples_rows += 1
            continue  # step data tables are not interpreted
        if (st := _keyword(line, STEP_KEYWORDS)) is not None:
            kw, step_text = st
            effective = previous_kw if kw in ("And", "But", "*") else kw
            previous_kw = effective
            step = Step(effective, step_text, n)
            if in_background:
                feature.background.append(step)
            elif current is not None:
                current.steps.append(step)
    return feature


def lint(feature: Feature) -> list[dict]:
    """Problems that are facts, not opinions. Each: scenario, line, code, message."""
    issues: list[dict] = []

    def add(sc: Scenario | None, line: int, code: str, message: str) -> None:
        issues.append({"scenario": sc.name if sc else "", "line": line,
                       "code": code, "message": message})

    if not feature.scenarios:
        add(None, 1, "no-scenarios", "The feature has no scenarios, so nothing can be tested.")
    seen: dict[str, int] = {}
    for sc in feature.scenarios:
        if not sc.name:
            add(sc, sc.line, "unnamed", "A scenario without a name can't be traced to a test.")
        elif sc.name.lower() in seen:
            add(sc, sc.line, "duplicate-name",
                f"Same name as the scenario on line {seen[sc.name.lower()]}; pytest-bdd "
                "can't tell them apart.")
        else:
            seen[sc.name.lower()] = sc.line
        kws = {s.keyword for s in sc.steps}
        if "Then" not in kws:
            add(sc, sc.line, "no-then", "No Then step: what outcome would prove this works?")
        if "When" not in kws:
            add(sc, sc.line, "no-when", "No When step: which action does this scenario test?")
        for s in sc.steps:
            if s.keyword == "Then" and (m := VAGUE.search(s.text)):
                add(sc, s.line, "vague-then",
                    f'"{m.group(0)}" in a Then step can\'t be checked by a test.')
        used = {p for s in sc.steps for p in PLACEHOLDER.findall(s.text)}
        if sc.outline:
            header = sc.examples_header or []
            if not sc.examples_rows:
                add(sc, sc.line, "no-examples", "Scenario Outline without example rows.")
            for p in sorted(used - set(header)):
                add(sc, sc.line, "unknown-placeholder",
                    f"<{p}> is used in a step but is not an Examples column.")
            for c in [c for c in header if c not in used]:
                add(sc, sc.line, "unused-column", f"Examples column '{c}' is never used.")
        elif used:
            add(sc, sc.line, "placeholder-in-scenario",
                f"<{sorted(used)[0]}> in a plain Scenario: should it be a Scenario Outline?")
    return issues


def python_name(name: str) -> str:
    """How pytest-bdd turns a scenario name into a test function name (minus `test_`)."""
    return re.sub(r"\W+", "_", name.lower()).strip("_")


def read_results(junit_xml: Path) -> dict[str, str]:
    """pytest --junitxml report -> {python name: passed|failed|skipped}. Outline rows
    (`test_x[...]`) fold into one entry; any failing row fails the scenario."""
    rank = {"passed": 0, "skipped": 1, "failed": 2}
    results: dict[str, str] = {}
    for case in ET.parse(junit_xml).getroot().iter("testcase"):
        name = re.sub(r"\[.*\]$", "", case.get("name", ""))
        name = python_name(name.removeprefix("test_"))
        if case.find("failure") is not None or case.find("error") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        if rank[status] >= rank[results.get(name, "passed")]:
            results[name] = status
    return results


def scenario_status(feature: Feature, results: dict[str, str]) -> dict[str, str]:
    """Each scenario: passed, failed, skipped, or not-automated (no test runs it)."""
    return {sc.name: results.get(python_name(sc.name), "not-automated")
            for sc in feature.scenarios}


def outline_text(feature: Feature) -> str:
    """The feature as numbered lines the model can point at (background first)."""
    lines = [f"Feature: {feature.name}"]
    if feature.background:
        lines.append("  Background:")
        lines += [f"    L{s.line} {s.keyword} {s.text}" for s in feature.background]
    for sc in feature.scenarios:
        kind = "Scenario Outline" if sc.outline else "Scenario"
        tags = f" {' '.join(sc.tags)}" if sc.tags else ""
        lines.append(f"  L{sc.line} {kind}: {sc.name}{tags}")
        lines += [f"    L{s.line} {s.keyword} {s.text}" for s in sc.steps]
        if sc.outline:
            lines.append(f"    Examples: {sc.examples_header or []} ({sc.examples_rows} rows)")
    return "\n".join(lines)
