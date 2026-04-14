from __future__ import annotations

import re
from dataclasses import dataclass

from .judge import Judge, JudgeContext
from .schema import Matcher


@dataclass
class CheckResult:
    type: str
    passed: bool
    detail: str
    skipped: bool = False


def run_matcher(
    matcher: Matcher,
    user_msg: str,
    bot_msg: str,
    judge: Judge,
    ctx: JudgeContext,
) -> CheckResult:
    t = matcher.type
    v = matcher.value

    if t == "contains":
        passed = v.lower() in bot_msg.lower()
        return CheckResult("contains", passed, f"needle={v!r}")

    if t == "not_contains":
        passed = v.lower() not in bot_msg.lower()
        return CheckResult("not_contains", passed, f"needle={v!r}")

    if t == "regex":
        try:
            passed = re.search(v, bot_msg, re.IGNORECASE | re.DOTALL) is not None
        except re.error as e:
            return CheckResult("regex", False, f"invalid regex: {e}")
        return CheckResult("regex", passed, f"pattern={v!r}")

    if t == "judge":
        if not judge.enabled:
            return CheckResult("judge", True, "skipped (no OPENAI_API_KEY)", skipped=True)
        passed, detail = judge.evaluate(user_msg, bot_msg, v, ctx)
        return CheckResult("judge", passed, detail)

    return CheckResult(t, False, f"unknown matcher type {t!r}")
