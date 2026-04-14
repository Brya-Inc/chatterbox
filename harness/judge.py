from __future__ import annotations

import datetime
from dataclasses import dataclass

from openai import OpenAI

from .config import Config
from .scraper import format_events


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

        header = f"You are evaluating a chatbot response. Today's date is {ctx.today}.\n\n"
        if ctx.events:
            header += "Events currently listed on the platform:\n" + format_events(ctx.events) + "\n\n"
        if ctx.my_events:
            header += "Events the user has RSVP'd to:\n" + format_events(ctx.my_events) + "\n\n"

        prompt = (
            f"{header}"
            f"User message:\n{user_msg}\n\n"
            f"Bot response:\n{bot_msg}\n\n"
            f"Evaluation criterion:\n{filled}\n\n"
            "Reply with PASS or FAIL on the first line, then a one-sentence explanation."
        )

        result = self.client.chat.completions.create(
            model=self.cfg.judge_model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (result.choices[0].message.content or "").strip()
        passed = text.upper().startswith("PASS")
        return passed, text
