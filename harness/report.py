from __future__ import annotations

import json
import sys
from pathlib import Path

from .runner import ConversationResult


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    if not _supports_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _label(r: ConversationResult) -> str:
    parts = [r.conversation.name]
    meta = []
    if r.user_email:
        meta.append(r.user_email)
    if r.browser:
        meta.append(r.browser)
    if meta:
        parts.append(f"({', '.join(meta)})")
    return " ".join(parts)


def print_summary(results: list[ConversationResult]) -> int:
    print(f"\n{'=' * 60}\nSUMMARY\n{'=' * 60}")
    total, passed = 0, 0
    failed_labels: list[str] = []
    skipped_labels: list[str] = []

    for r in results:
        label = _label(r)
        if r.skipped_reason:
            skipped_labels.append(label)
            print(f"  {_c('SKIP', '33')}  {label} — {r.skipped_reason}")
            continue

        tag = _c("PASS", "32") if r.passed else _c("FAIL", "31")
        print(f"  {tag}  {label}")
        if not r.passed:
            failed_labels.append(label)

        for t in r.turns:
            if t.error:
                print(f"     turn {t.index}: ERROR {t.error[:120]}")
                continue
            for c in t.checks:
                total += 1
                if c.skipped:
                    continue
                if c.passed:
                    passed += 1
                else:
                    print(f"     turn {t.index} {c.type}: {c.detail[:160]}")
        if r.final_check and not r.final_check.skipped:
            total += 1
            if r.final_check.passed:
                passed += 1
            else:
                print(f"     final {r.final_check.type}: {r.final_check.detail[:160]}")

    ran = len(results) - len(skipped_labels)
    ok = ran - len(failed_labels)
    print(f"\nChecks : {passed}/{total} passed")
    print(f"Runs   : {ok}/{ran} passed ({len(skipped_labels)} skipped)")
    return 0 if not failed_labels and not skipped_labels else 1


def write_json(results: list[ConversationResult], path: Path) -> None:
    payload = []
    for r in results:
        payload.append(
            {
                "name": r.conversation.name,
                "path": str(r.conversation.path),
                "tags": r.conversation.tags,
                "user_email": r.user_email,
                "browser": r.browser,
                "passed": r.passed,
                "skipped_reason": r.skipped_reason,
                "turns": [
                    {
                        "index": t.index,
                        "send": t.send,
                        "response": t.response,
                        "error": t.error,
                        "checks": [
                            {
                                "type": c.type,
                                "passed": c.passed,
                                "skipped": c.skipped,
                                "detail": c.detail,
                            }
                            for c in t.checks
                        ],
                    }
                    for t in r.turns
                ],
                "final": (
                    {
                        "type": r.final_check.type,
                        "passed": r.final_check.passed,
                        "skipped": r.final_check.skipped,
                        "detail": r.final_check.detail,
                    }
                    if r.final_check
                    else None
                ),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    print(f"\nJSON report written to {path}")
