from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from .config import Config
from .scraper import format_events

_PLAYBOOK_PATH = Path(__file__).parent / "judge_playbook.md"


@dataclass
class JudgeContext:
    events: list[dict]
    my_events: list[dict]

    @property
    def today(self) -> str:
        return datetime.date.today().strftime("%Y-%m-%d")


class Judge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client: OpenAI | None = OpenAI(api_key=cfg.openai_api_key) if cfg.openai_api_key else None
        self._playbook = _PLAYBOOK_PATH.read_text() if _PLAYBOOK_PATH.exists() else ""

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def resolve_placeholders(self, text: str, ctx: JudgeContext) -> str:
        return (
            text.replace("{TODAY}", ctx.today)
            .replace("{EVENTS}", format_events(ctx.events))
            .replace("{MY_EVENTS}", format_events(ctx.my_events))
        )

    def evaluate(
        self,
        user_msg: str,
        bot_msg: str,
        criterion: str,
        ctx: JudgeContext,
    ) -> tuple[bool, str]:
        assert self.client is not None
        filled = self.resolve_placeholders(criterion, ctx)

        context_block = f"Today's date is {ctx.today}.\n\n"
        if ctx.events:
            context_block += "Events currently listed on the platform:\n" + format_events(ctx.events) + "\n\n"
        if ctx.my_events:
            context_block += "Events the user has RSVP'd to:\n" + format_events(ctx.my_events) + "\n\n"

        user_prompt = (
            f"{context_block}"
            f"User message:\n{user_msg}\n\n"
            f"Bot response:\n{bot_msg}\n\n"
            f"Evaluation criterion:\n{filled}\n\n"
            "Reply with PASS or FAIL on the first line, then a one-sentence explanation."
        )

        messages = []
        if self._playbook:
            messages.append({"role": "system", "content": self._playbook})
        messages.append({"role": "user", "content": user_prompt})

        result = self.client.chat.completions.create(
            model=self.cfg.judge_model,
            max_tokens=256,
            messages=messages,
        )
        text = (result.choices[0].message.content or "").strip()
        passed = text.upper().startswith("PASS")
        return passed, text
