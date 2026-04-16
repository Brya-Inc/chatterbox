from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from .auth import ensure_logged_in
from .chat_driver import ChatDriver
from .config import BROWSER_ENGINES, Config, UserCreds
from .judge import Judge, JudgeContext
from .matchers import CheckResult, run_matcher
from .schema import Conversation, Matcher
from .scraper import scrape_home_events, scrape_my_rsvps
from .test_config import DirConfig, load_dir_config


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
    user_email: str = ""
    browser: str = ""

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
        """Build a per-browser schedule, then launch each browser once and run its queue."""
        groups: dict[Path, list[Conversation]] = {}
        for conv in conversations:
            groups.setdefault(conv.path.parent, []).append(conv)

        dir_configs: dict[Path, DirConfig] = {d: load_dir_config(d) for d in groups}

        # Per-test browsers can differ (secondary rotates), so we build a per-browser
        # schedule of (conv, user, source_dir) tuples.
        schedule: dict[str, list[tuple[Conversation, UserCreds, Path]]] = {}
        for dir_path, convs in groups.items():
            dc = dir_configs[dir_path]
            for test_index, conv in enumerate(convs):
                browsers_here = dc.browsers_to_run(self.cfg, test_index)
                users_here = dc.users_for_test(test_index, self.cfg)
                for b in browsers_here:
                    for u in users_here:
                        schedule.setdefault(b, []).append((conv, u, dir_path))

        if not schedule:
            print("WARNING: no browsers to run — check per-directory config.yaml or --browsers flag")
            return []

        all_browsers = sorted(schedule.keys())
        total_runs = sum(len(q) for q in schedule.values())
        print(f"Browsers: {all_browsers}  |  Total runs: {total_runs}")

        results: list[ConversationResult] = []
        with sync_playwright() as pw:
            for browser_name in all_browsers:
                engine, channel = BROWSER_ENGINES.get(browser_name, ("chromium", None))
                print(f"\n{'=' * 60}\n=== Browser: {browser_name} (engine={engine}{f', channel={channel}' if channel else ''})\n{'=' * 60}")
                try:
                    browser = self._launch_browser(pw, engine, channel)
                except Exception as e:
                    print(f"  [browser] failed to launch {browser_name}: {e}")
                    continue
                try:
                    current_dir: Path | None = None
                    for conv, user, dir_path in schedule[browser_name]:
                        if dir_path != current_dir:
                            print(f"\n{'#' * 60}\n# Directory: {dir_path}\n{'#' * 60}")
                            current_dir = dir_path
                        results.append(self._run_for_user(browser, conv, user, browser_name))
                finally:
                    browser.close()
        return results

    def _launch_browser(self, pw: Playwright, engine: str, channel: str | None) -> Browser:
        launcher = {"chromium": pw.chromium, "firefox": pw.firefox, "webkit": pw.webkit}[engine]
        kwargs: dict = {"headless": self.cfg.headless}
        if channel:
            kwargs["channel"] = channel
        return launcher.launch(**kwargs)

    def _run_for_user(
        self,
        browser: Browser,
        conv: Conversation,
        user: UserCreds,
        browser_name: str,
    ) -> ConversationResult:
        """Open a fresh context for `user` on `browser`, log in, run, close."""
        print(f"\n{'-' * 60}\n[browser={browser_name}] [user={user.email}]\n{'-' * 60}")
        context = self._new_context_for(browser, user, browser_name)
        page = context.new_page()
        try:
            ensure_logged_in(context, page, self.cfg, user, browser_name)
            page.goto(f"{self.cfg.base_url}/loggedInHome")
            page.wait_for_load_state("load")

            driver = ChatDriver(page, self.cfg)
            driver.open_chat()

            events = scrape_home_events(page)
            my_events = scrape_my_rsvps(page)
            print(f"  [scrape] {len(events)} home events, {len(my_events)} RSVP'd")

            if conv.setup.admin_clear:
                driver.clear_chat_admin()
                driver.open_chat()
                events = scrape_home_events(page)
                my_events = scrape_my_rsvps(page)
            elif conv.setup.fresh_chat:
                target = conv.url or f"{self.cfg.base_url}/loggedInHome"
                page.goto(target)
                page.wait_for_load_state("load")
                driver.open_chat()
                events = scrape_home_events(page)
                my_events = scrape_my_rsvps(page)

            ctx = JudgeContext(events=events, my_events=my_events)
            result = self._run_one(driver, conv, ctx)
            result.user_email = user.email
            result.browser = browser_name
            return result
        except Exception as e:
            print(f"  [runner] error during {conv.name} as {user.email} on {browser_name}: {e}")
            res = ConversationResult(
                conversation=conv, user_email=user.email, browser=browser_name
            )
            res.turns.append(TurnResult(index=0, send="", response="", error=str(e)))
            return res
        finally:
            context.close()

    def _new_context_for(self, browser: Browser, user: UserCreds, browser_name: str) -> BrowserContext:
        state_path = self.cfg.storage_path_for(user, browser_name)
        if state_path.exists():
            return browser.new_context(storage_state=str(state_path))
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
