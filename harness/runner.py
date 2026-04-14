from __future__ import annotations

from dataclasses import dataclass, field

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .auth import ensure_logged_in
from .chat_driver import ChatDriver
from .config import Config
from .judge import Judge, JudgeContext
from .matchers import CheckResult, run_matcher
from .schema import Conversation, Matcher
from .scraper import scrape_home_events, scrape_my_rsvps


@dataclass
class TurnResult:
    index: int
    send: str
    response: str
    checks: list[CheckResult] = field(default_factory=list)
    error: str | None = None


@dataclass
class ConversationResult:
    conversation: Conversation
    turns: list[TurnResult] = field(default_factory=list)
    final_check: CheckResult | None = None
    skipped_reason: str | None = None

    @property
    def passed(self) -> bool:
        if self.skipped_reason:
            return False
        for t in self.turns:
            if t.error:
                return False
            for c in t.checks:
                if not c.skipped and not c.passed:
                    return False
        if self.final_check and not self.final_check.skipped and not self.final_check.passed:
            return False
        return True


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.judge = Judge(cfg)

    def run(self, conversations: list[Conversation]) -> list[ConversationResult]:
        results: list[ConversationResult] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.cfg.headless)
            context = self._new_context(browser)
            page = context.new_page()

            try:
                ensure_logged_in(context, page, self.cfg)
                page.goto(f"{self.cfg.base_url}/loggedInHome")
                page.wait_for_load_state("load")

                driver = ChatDriver(page, self.cfg)
                driver.open_chat()

                events = scrape_home_events(page)
                my_events = scrape_my_rsvps(page)
                print(f"  [scrape] {len(events)} home events, {len(my_events)} RSVP'd")

                for conv in conversations:
                    if conv.setup.fresh_chat:
                        target = conv.url or f"{self.cfg.base_url}/loggedInHome"
                        page.goto(target)
                        page.wait_for_load_state("load")
                        driver.open_chat()
                        events = scrape_home_events(page)
                        my_events = scrape_my_rsvps(page)

                    ctx = JudgeContext(events=events, my_events=my_events)
                    results.append(self._run_one(driver, conv, ctx))
            finally:
                browser.close()
        return results

    def _new_context(self, browser: Browser) -> BrowserContext:
        if self.cfg.storage_state_path.exists():
            return browser.new_context(storage_state=str(self.cfg.storage_state_path))
        return browser.new_context()

    def _run_one(
        self,
        driver: ChatDriver,
        conv: Conversation,
        ctx: JudgeContext,
    ) -> ConversationResult:
        print(f"\n{'=' * 60}\nTest : {conv.name}\n{'=' * 60}")

        if conv.setup.require_events and not ctx.events:
            print("  [skip] require_events set but scraped 0 events")
            return ConversationResult(
                conversation=conv,
                skipped_reason="require_events: no events scraped from home page",
            )

        result = ConversationResult(conversation=conv)
        transcript: list[tuple[str, str]] = []

        for i, turn in enumerate(conv.turns, start=1):
            print(f"\n  [Turn {i}] ▶  {turn.send!r}")
            try:
                response = driver.send(turn.send)
            except RuntimeError as e:
                print(f"  [Turn {i}] ERROR: {e}")
                result.turns.append(TurnResult(index=i, send=turn.send, response="", error=str(e)))
                return result

            preview = response[:120] + ("…" if len(response) > 120 else "")
            print(f"  [Turn {i}] ◀  {preview!r}")

            # Re-scrape MY_EVENTS between turns in case the bot mutated state.
            ctx = JudgeContext(events=ctx.events, my_events=scrape_my_rsvps(driver.page))

            checks: list[CheckResult] = []
            for matcher in turn.expect:
                check = run_matcher(matcher, turn.send, response, self.judge, ctx)
                checks.append(check)
                tag = "SKIP" if check.skipped else ("PASS" if check.passed else "FAIL")
                print(f"  [Turn {i}] {matcher.type}: {tag} — {_first_line(check.detail)}")

            result.turns.append(
                TurnResult(index=i, send=turn.send, response=response, checks=checks)
            )
            transcript.append((turn.send, response))

        if conv.final:
            full_user = "\n---\n".join(u for u, _ in transcript)
            full_bot = "\n---\n".join(b for _, b in transcript)
            ctx = JudgeContext(events=ctx.events, my_events=scrape_my_rsvps(driver.page))
            result.final_check = run_matcher(conv.final, full_user, full_bot, self.judge, ctx)
            tag = "SKIP" if result.final_check.skipped else ("PASS" if result.final_check.passed else "FAIL")
            print(f"\n  [final] {conv.final.type}: {tag} — {_first_line(result.final_check.detail)}")

        return result


def _first_line(s: str) -> str:
    return (s.splitlines()[0] if s else "")[:160]
