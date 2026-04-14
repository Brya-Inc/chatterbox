from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MATCHER_TYPES = {"contains", "not_contains", "regex", "judge"}


@dataclass
class Matcher:
    type: str
    value: str


@dataclass
class Turn:
    send: str
    expect: list[Matcher] = field(default_factory=list)


@dataclass
class Setup:
    fresh_chat: bool = False
    require_events: bool = False


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
    )

    turns_raw = data.get("turns") or []
    if not isinstance(turns_raw, list) or not turns_raw:
        raise SchemaError(f"{path}: 'turns' must be a non-empty list")

    turns: list[Turn] = []
    for i, traw in enumerate(turns_raw):
        if not isinstance(traw, dict) or "send" not in traw:
            raise SchemaError(f"{path}: turns[{i}] must have a 'send' key")
        send = traw["send"]
        if not isinstance(send, str):
            raise SchemaError(f"{path}: turns[{i}].send must be a string")
        expect = _parse_matcher_list(traw.get("expect"), f"{path}: turns[{i}].expect")
        turns.append(Turn(send=send, expect=expect))

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
    return sorted(p for p in root.rglob("*.yaml"))
