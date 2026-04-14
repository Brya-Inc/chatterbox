#!/usr/bin/env python3
"""
Chatterbox — Playwright-driven test harness for the Brya chatbot (Sage).

Usage:
    python run_tests.py                        # run all conversations
    python run_tests.py path/to/conv.yaml      # run specific file(s)
    python run_tests.py --tag smoke            # run only conversations tagged 'smoke'
    python run_tests.py --json out/report.json # also write a JSON report
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harness.config import load_config
from harness.report import print_summary, write_json
from harness.runner import Runner
from harness.schema import Conversation, SchemaError, discover_conversations, load_conversation


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run chatterbox conversation tests")
    p.add_argument("paths", nargs="*", type=Path, help="Conversation YAML files or directories")
    p.add_argument("--tag", action="append", default=[], help="Only run conversations with this tag (repeatable)")
    p.add_argument("--json", type=Path, default=None, help="Write a JSON report to this path")
    return p.parse_args()


def _collect_paths(paths: list[Path], root: Path) -> list[Path]:
    if not paths:
        return discover_conversations(root)
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(p.rglob("*.yaml")))
        elif p.suffix in (".yaml", ".yml"):
            out.append(p)
        else:
            print(f"ERROR: not a YAML file or directory: {p}", file=sys.stderr)
            sys.exit(2)
    return out


def _filter_tags(convs: list[Conversation], tags: list[str]) -> list[Conversation]:
    if not tags:
        return convs
    wanted = set(tags)
    return [c for c in convs if wanted.intersection(c.tags)]


def main() -> int:
    args = parse_args()
    cfg = load_config()

    files = _collect_paths(args.paths, cfg.conversations_dir)
    if not files:
        print(f"No conversation files found under {cfg.conversations_dir}")
        return 2

    try:
        conversations = [load_conversation(f) for f in files]
    except SchemaError as e:
        print(f"Schema error: {e}", file=sys.stderr)
        return 2

    conversations = _filter_tags(conversations, args.tag)
    if not conversations:
        print(f"No conversations match tag filter {args.tag}")
        return 2

    print(f"Target : {cfg.base_url}")
    print(f"Loaded : {len(conversations)} conversation(s)")
    if not cfg.openai_api_key:
        print("Note   : OPENAI_API_KEY not set — judge checks will be skipped")

    runner = Runner(cfg)
    results = runner.run(conversations)

    exit_code = print_summary(results)
    if args.json:
        write_json(results, args.json)
    return exit_code


if __name__ == "__main__":
    code = main()
    print(f"\nExit code: {code}")
    # Skip sys.exit when a debugger is attached so the IDE doesn't surface
    # SystemExit as an uncaught exception. CLI/CI runs still get a real
    # non-zero exit code.
    if sys.gettrace() is None:
        sys.exit(code)
