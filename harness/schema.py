from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MATCHER_TYPES = {"contains", "not_contains", "regex", "judge"}

# Step kinds:
#   SEND: talk to the Sage chatbot (existing behavior).
#   BROWSER: generic browser actions — click, navigate, fill, etc.
#   ASSERT: page-state checks that pass/fail the test.
SEND_STEPS = {"send"}
BROWSER_STEPS = {
    "navigate", "click", "fill", "press_key",
    "refresh", "back", "wait", "wait_for",
    "scroll",
    "open_tab", "switch_tab", "close_tab",
}
ASSERT_STEPS = {
    "assert_visible", "assert_not_visible",
    "assert_text", "assert_url",
    "assert_focused", "assert_not_focused",
    "assert_count",
    "assert_no_overflow", "assert_no_overlap", "assert_style",
    "assert_response_within",
}
STEP_TYPES = SEND_STEPS | BROWSER_STEPS | ASSERT_STEPS

# Steps that take no value (value must be None).
NO_VALUE_STEPS = {"refresh", "back"}
# Steps whose value is a dict with named fields.
DICT_VALUE_STEPS = {"fill", "assert_text", "scroll", "assert_count",
                    "assert_no_overlap", "assert_style"}

# Operators accepted by `assert_text`.
ASSERT_TEXT_OPS = {"contains", "starts_with", "ends_with", "equals"}


@dataclass
class Matcher:
    type: str
    value: str


@dataclass
class Turn:
    """A single step in the conversation: a chat message, a browser action,
    or a page-state assertion."""
    type: str = "send"
    value: Any = ""
    expect: list[Matcher] = field(default_factory=list)

    @property
    def send(self) -> str:
        """Back-compat: old code reads turn.send for the message text."""
        return self.value if self.type == "send" and isinstance(self.value, str) else ""


@dataclass
class Setup:
    fresh_chat: bool = False
    require_events: bool = False
    admin_clear: bool = False  # click Admin → "Start fresh conversation" before test


@dataclass
class Conversation:
    path: Path
    name: str
    description: str
    tags: list[str]
    url: str | None
    setup: Setup
    turns: list[Turn]
    final: Matcher | None


class SchemaError(ValueError):
    pass


def _parse_matcher_list(raw: Any, where: str) -> list[Matcher]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SchemaError(f"{where}: expected a list of matchers, got {type(raw).__name__}")
    out: list[Matcher] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or len(item) != 1:
            raise SchemaError(
                f"{where}[{i}]: each matcher must be a single-key mapping "
                f"(one of {sorted(MATCHER_TYPES)})"
            )
        (mtype, mval), = item.items()
        if mtype not in MATCHER_TYPES:
            raise SchemaError(
                f"{where}[{i}]: unknown matcher type '{mtype}' "
                f"(supported: {sorted(MATCHER_TYPES)})"
            )
        if not isinstance(mval, str) or not mval.strip():
            raise SchemaError(f"{where}[{i}]: matcher '{mtype}' needs a non-empty string value")
        out.append(Matcher(type=mtype, value=mval))
    return out


def _parse_final(raw: Any, where: str) -> Matcher | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or len(raw) != 1:
        raise SchemaError(f"{where}: 'final' must be a single-key mapping (e.g. judge: ...)")
    (mtype, mval), = raw.items()
    if mtype not in MATCHER_TYPES:
        raise SchemaError(f"{where}: unknown final matcher '{mtype}'")
    return Matcher(type=mtype, value=mval)


def _parse_turn(traw: Any, where: str) -> Turn:
    """Parse one turn: figure out which step type it is and validate."""
    # Bare-string form for no-value steps: `- back`, `- refresh`.
    if isinstance(traw, str):
        if traw in NO_VALUE_STEPS:
            return Turn(type=traw, value=None)
        raise SchemaError(
            f"{where}: bare string {traw!r} is only valid for no-value steps "
            f"({sorted(NO_VALUE_STEPS)})"
        )
    if not isinstance(traw, dict):
        raise SchemaError(f"{where}: turn must be a mapping or one of {sorted(NO_VALUE_STEPS)}")

    # Find which step type this is. Every turn has exactly one action key
    # (optionally plus an `expect` key for the send step).
    step_keys = [k for k in traw.keys() if k in STEP_TYPES]
    if len(step_keys) == 0:
        raise SchemaError(
            f"{where}: turn has no recognized step key "
            f"(expected one of {sorted(STEP_TYPES)})"
        )
    if len(step_keys) > 1:
        raise SchemaError(
            f"{where}: turn has multiple step keys {step_keys} — use only one"
        )
    step_type = step_keys[0]
    value = traw[step_type]

    # Validate value shape per step type.
    if step_type in NO_VALUE_STEPS:
        # `refresh` and `back` — value is ignored (or can be None).
        value = None
    elif step_type in DICT_VALUE_STEPS:
        if not isinstance(value, dict):
            raise SchemaError(f"{where}: '{step_type}' value must be a mapping")
        if step_type == "fill":
            if "selector" not in value or "value" not in value:
                raise SchemaError(f"{where}: 'fill' needs 'selector' and 'value' keys")
        elif step_type == "assert_text":
            if "selector" not in value:
                raise SchemaError(f"{where}: 'assert_text' needs a 'selector' key")
            ops_present = [op for op in ASSERT_TEXT_OPS if op in value]
            if len(ops_present) == 0:
                raise SchemaError(
                    f"{where}: 'assert_text' needs one of {sorted(ASSERT_TEXT_OPS)}"
                )
            if len(ops_present) > 1:
                raise SchemaError(
                    f"{where}: 'assert_text' can only use one operator at a time, got {ops_present}"
                )
        elif step_type == "assert_no_overlap":
            if "a" not in value or "b" not in value:
                raise SchemaError(f"{where}: 'assert_no_overlap' needs 'a' and 'b' keys (both selectors)")
        elif step_type == "assert_style":
            if "selector" not in value or "property" not in value:
                raise SchemaError(f"{where}: 'assert_style' needs 'selector' and 'property' keys")
            if not any(op in value for op in ("equals", "not_equals", "contains")):
                raise SchemaError(
                    f"{where}: 'assert_style' needs one of 'equals', 'not_equals', 'contains'"
                )
        elif step_type == "scroll":
            if "to" not in value and "by" not in value:
                raise SchemaError(f"{where}: 'scroll' needs 'to' (top|bottom) or 'by' (pixels)")
            if "to" in value and value["to"] not in ("top", "bottom"):
                raise SchemaError(f"{where}: 'scroll.to' must be 'top' or 'bottom'")
        elif step_type == "assert_count":
            if "selector" not in value or "at_least" not in value:
                raise SchemaError(f"{where}: 'assert_count' needs 'selector' and 'at_least' keys")
    elif step_type == "wait":
        # wait: value is ms (int) or seconds (float).
        if not isinstance(value, (int, float)):
            raise SchemaError(f"{where}: 'wait' value must be a number of milliseconds")
    elif step_type == "assert_response_within":
        if not isinstance(value, (int, float)):
            raise SchemaError(f"{where}: 'assert_response_within' value must be milliseconds")
    else:
        # Single-string-value steps (send, navigate, click, press_key, wait_for,
        # assert_visible, assert_not_visible, assert_url).
        if not isinstance(value, str):
            raise SchemaError(f"{where}: '{step_type}' value must be a string")

    # `expect` is only meaningful for `send` (evaluates Sage's response).
    expect: list[Matcher] = []
    if "expect" in traw:
        if step_type != "send":
            raise SchemaError(
                f"{where}: 'expect' is only valid on 'send' steps, not '{step_type}'"
            )
        expect = _parse_matcher_list(traw["expect"], f"{where}.expect")

    return Turn(type=step_type, value=value, expect=expect)


def load_conversation(path: Path) -> Conversation:
    with path.open() as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise SchemaError(f"{path}: top-level YAML must be a mapping")

    name = data.get("name") or path.stem
    description = data.get("description", "")
    tags = data.get("tags", []) or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise SchemaError(f"{path}: 'tags' must be a list of strings")
    url = data.get("url")

    setup_raw = data.get("setup") or {}
    if not isinstance(setup_raw, dict):
        raise SchemaError(f"{path}: 'setup' must be a mapping")
    setup = Setup(
        fresh_chat=bool(setup_raw.get("fresh_chat", False)),
        require_events=bool(setup_raw.get("require_events", False)),
        admin_clear=bool(setup_raw.get("admin_clear", False)),
    )

    turns_raw = data.get("turns") or []
    if not isinstance(turns_raw, list) or not turns_raw:
        raise SchemaError(f"{path}: 'turns' must be a non-empty list")

    turns: list[Turn] = []
    for i, traw in enumerate(turns_raw):
        turns.append(_parse_turn(traw, f"{path}: turns[{i}]"))

    final = _parse_final(data.get("final"), str(path))

    return Conversation(
        path=path,
        name=name,
        description=description,
        tags=tags,
        url=url,
        setup=setup,
        turns=turns,
        final=final,
    )


def discover_conversations(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.yaml") if p.name != "config.yaml")
