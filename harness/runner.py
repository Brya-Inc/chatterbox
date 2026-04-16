from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

import re
import time

from .auth import ensure_logged_in
from .chat_driver import ChatDriver
from .config import BROWSER_ENGINES, Config, UserCreds
from .judge import Judge, JudgeContext
from .matchers import CheckResult, run_matcher
from .schema import Conversation, Matcher, Turn
from .scraper import scrape_home_events, scrape_my_rsvps
from .test_config import DirConfig, load_dir_config


@dataclass
class TurnResult:
    index: int
    send: str
    response: str
    checks: list[CheckResult] = field(default_factory=list)
    error: str | None = None
    response_ms: int | None = None  # how long the `send` took; only set on send turns


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

        # Multi-tab state. The default page is implicitly named "default".
        tabs: dict[str, Page] = {"default": driver.page}
        current_tab = "default"

        last_send_ms: int | None = None
        for i, turn in enumerate(conv.turns, start=1):
            try:
                # Tab-management steps are handled inline because they mutate `tabs` / `current_tab`.
                if turn.type == "open_tab":
                    name = turn.value
                    if name in tabs:
                        raise RuntimeError(f"tab {name!r} already open")
                    new_page = driver.page.context.new_page()
                    tabs[name] = new_page
                    current_tab = name
                    print(f"\n  [Turn {i}] ▶  open_tab: {name!r} (now current)")
                    result.turns.append(TurnResult(index=i, send=f"open_tab {name!r}", response=""))
                    continue

                if turn.type == "switch_tab":
                    name = turn.value
                    if name not in tabs:
                        raise RuntimeError(f"tab {name!r} not open; available: {sorted(tabs)}")
                    current_tab = name
                    tabs[name].bring_to_front()
                    print(f"\n  [Turn {i}] ▶  switch_tab: {name!r}")
                    result.turns.append(TurnResult(index=i, send=f"switch_tab {name!r}", response=""))
                    continue

                if turn.type == "close_tab":
                    name = turn.value
                    if name == "default":
                        raise RuntimeError("cannot close the default tab")
                    if name in tabs:
                        tabs[name].close()
                        del tabs[name]
                        if current_tab == name:
                            current_tab = "default"
                    print(f"\n  [Turn {i}] ▶  close_tab: {name!r}")
                    result.turns.append(TurnResult(index=i, send=f"close_tab {name!r}", response=""))
                    continue

                if turn.type == "send":
                    print(f"\n  [Turn {i}] ▶  send: {turn.value!r}")
                    start = time.time()
                    response = driver.send(turn.value)
                    last_send_ms = int((time.time() - start) * 1000)
                    preview = response[:120] + ("…" if len(response) > 120 else "")
                    print(f"  [Turn {i}] ◀  {preview!r}  ({last_send_ms}ms)")

                    ctx = JudgeContext(events=ctx.events, my_events=scrape_my_rsvps(driver.page))
                    checks: list[CheckResult] = []
                    for matcher in turn.expect:
                        check = run_matcher(matcher, turn.value, response, self.judge, ctx)
                        checks.append(check)
                        tag = "SKIP" if check.skipped else ("PASS" if check.passed else "FAIL")
                        print(f"  [Turn {i}] {matcher.type}: {tag} — {_first_line(check.detail)}")

                    result.turns.append(
                        TurnResult(
                            index=i, send=turn.value, response=response,
                            checks=checks, response_ms=last_send_ms,
                        )
                    )
                    transcript.append((turn.value, response))
                else:
                    # Browser action or page-state assertion — run against the currently active tab.
                    active_page = tabs[current_tab]
                    action_desc, maybe_check = _execute_non_send(
                        active_page, turn, self.cfg, last_send_ms
                    )
                    print(f"\n  [Turn {i}] ▶  {turn.type}: {action_desc}")
                    tres = TurnResult(index=i, send=action_desc, response="")
                    if maybe_check is not None:
                        tres.checks = [maybe_check]
                        tag = "PASS" if maybe_check.passed else "FAIL"
                        print(f"  [Turn {i}] {turn.type}: {tag} — {_first_line(maybe_check.detail)}")
                        # Stop the test on an assertion failure so later steps don't run on bad state.
                        result.turns.append(tres)
                        if not maybe_check.passed:
                            return result
                        continue
                    result.turns.append(tres)
            except Exception as e:
                print(f"  [Turn {i}] ERROR: {e}")
                result.turns.append(
                    TurnResult(index=i, send=str(turn.value or turn.type), response="", error=str(e))
                )
                return result

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


def _execute_non_send(page, turn: Turn, cfg: Config, last_send_ms: int | None = None) -> tuple[str, CheckResult | None]:
    """
    Run a non-`send` step. Returns (human-readable description, optional CheckResult).
    CheckResult is only returned for assertion steps (assert_*).
    Raises on browser-action errors; those are caught by the caller and recorded.
    """
    t = turn.type
    v = turn.value

    # Browser navigation / interaction
    if t == "navigate":
        url = v if v.startswith("http") else f"{cfg.base_url}{v if v.startswith('/') else '/' + v}"
        page.goto(url)
        page.wait_for_load_state("domcontentloaded")
        return (f"navigate {url}", None)

    if t == "click":
        page.click(v)
        return (f"click {v!r}", None)

    if t == "fill":
        page.fill(v["selector"], v["value"])
        return (f"fill {v['selector']!r} = {v['value']!r}", None)

    if t == "press_key":
        page.keyboard.press(v)
        return (f"press {v}", None)

    if t == "refresh":
        page.reload()
        page.wait_for_load_state("domcontentloaded")
        return ("browser refresh", None)

    if t == "back":
        page.go_back()
        page.wait_for_load_state("domcontentloaded")
        return ("browser back", None)

    if t == "wait":
        ms = int(v)
        page.wait_for_timeout(ms)
        return (f"wait {ms}ms", None)

    if t == "wait_for":
        page.wait_for_selector(v, state="visible", timeout=15000)
        return (f"wait_for {v!r}", None)

    if t == "scroll":
        selector = v.get("selector")
        to = v.get("to")
        by = v.get("by")
        if selector:
            # Scroll a specific element's scroll container.
            if to == "bottom":
                page.evaluate(
                    "(s) => { const el = document.querySelector(s); if (el) el.scrollTop = el.scrollHeight; }",
                    selector,
                )
                desc = f"scroll {selector!r} to bottom"
            elif to == "top":
                page.evaluate(
                    "(s) => { const el = document.querySelector(s); if (el) el.scrollTop = 0; }",
                    selector,
                )
                desc = f"scroll {selector!r} to top"
            else:
                page.evaluate(
                    "({s, dy}) => { const el = document.querySelector(s); if (el) el.scrollTop += dy; }",
                    {"s": selector, "dy": int(by)},
                )
                desc = f"scroll {selector!r} by {by}px"
        else:
            # Scroll the window.
            if to == "bottom":
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                desc = "scroll window to bottom"
            elif to == "top":
                page.evaluate("() => window.scrollTo(0, 0)")
                desc = "scroll window to top"
            else:
                page.evaluate("(y) => window.scrollBy(0, y)", int(by))
                desc = f"scroll window by {by}px"
        return (desc, None)

    # Assertions — produce a CheckResult that passes/fails the test.
    if t == "assert_visible":
        visible = page.is_visible(v)
        return (
            f"assert_visible {v!r}",
            CheckResult(
                type=t,
                passed=visible,
                detail="" if visible else f"selector {v!r} is not visible",
            ),
        )

    if t == "assert_not_visible":
        visible = page.is_visible(v)
        return (
            f"assert_not_visible {v!r}",
            CheckResult(
                type=t,
                passed=not visible,
                detail="" if not visible else f"selector {v!r} is unexpectedly visible",
            ),
        )

    if t == "assert_text":
        sel = v["selector"]
        # One of: contains, starts_with, ends_with, equals.
        op = next(o for o in ("contains", "starts_with", "ends_with", "equals") if o in v)
        needle = v[op]
        try:
            text = page.inner_text(sel, timeout=5000)
        except Exception as e:
            return (
                f"assert_text {sel!r} {op} {needle!r}",
                CheckResult(type=t, passed=False, detail=f"could not read {sel!r}: {e}"),
            )
        text_lc = text.lower().strip()
        needle_lc = needle.lower().strip()
        if op == "contains":
            ok = needle_lc in text_lc
        elif op == "starts_with":
            ok = text_lc.startswith(needle_lc)
        elif op == "ends_with":
            ok = text_lc.endswith(needle_lc)
        else:  # equals
            ok = text_lc == needle_lc
        return (
            f"assert_text {sel!r} {op} {needle!r}",
            CheckResult(
                type=t,
                passed=ok,
                detail="" if ok else f"{sel!r} had text {text!r}; expected {op}={needle!r}",
            ),
        )

    if t == "assert_url":
        pat = v
        url = page.url
        ok = bool(re.search(pat, url))
        return (
            f"assert_url match /{pat}/",
            CheckResult(
                type=t,
                passed=ok,
                detail="" if ok else f"current URL {url!r} did not match pattern /{pat}/",
            ),
        )

    if t == "assert_response_within":
        max_ms = int(v)
        if last_send_ms is None:
            return (
                f"assert_response_within {max_ms}ms",
                CheckResult(
                    type=t, passed=False,
                    detail="no previous `send` to measure — use this after a send step",
                ),
            )
        ok = last_send_ms <= max_ms
        return (
            f"assert_response_within {max_ms}ms",
            CheckResult(
                type=t, passed=ok,
                detail="" if ok else f"last send took {last_send_ms}ms, expected <= {max_ms}ms",
            ),
        )

    if t == "assert_count":
        sel = v["selector"]
        need = int(v["at_least"])
        actual = page.locator(sel).count()
        ok = actual >= need
        return (
            f"assert_count {sel!r} at_least={need}",
            CheckResult(
                type=t,
                passed=ok,
                detail="" if ok else f"only {actual} elements match {sel!r} (needed {need})",
            ),
        )

    if t == "assert_no_overflow":
        # Element's content fits within its visible box (no horizontal/vertical clipping).
        info = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                // 1px tolerance for sub-pixel rounding.
                const overflowsX = el.scrollWidth > el.clientWidth + 1;
                const overflowsY = el.scrollHeight > el.clientHeight + 1;
                return {
                    overflowsX, overflowsY,
                    scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
                    scrollHeight: el.scrollHeight, clientHeight: el.clientHeight,
                    text: (el.innerText || '').trim().slice(0, 80),
                };
            }""",
            v,
        )
        if info is None:
            return (
                f"assert_no_overflow {v!r}",
                CheckResult(type=t, passed=False, detail=f"element {v!r} not found"),
            )
        overflows = info["overflowsX"] or info["overflowsY"]
        return (
            f"assert_no_overflow {v!r}",
            CheckResult(
                type=t,
                passed=not overflows,
                detail="" if not overflows else (
                    f"{v!r} overflows: "
                    f"scrollWidth={info['scrollWidth']} vs clientWidth={info['clientWidth']}, "
                    f"scrollHeight={info['scrollHeight']} vs clientHeight={info['clientHeight']} "
                    f"(text: {info['text']!r})"
                ),
            ),
        )

    if t == "assert_no_overlap":
        result = page.evaluate(
            """({a, b}) => {
                const ea = document.querySelector(a);
                const eb = document.querySelector(b);
                if (!ea || !eb) return {missing: true, aFound: !!ea, bFound: !!eb};
                const ra = ea.getBoundingClientRect();
                const rb = eb.getBoundingClientRect();
                const overlapping = !(ra.right <= rb.left || rb.right <= ra.left ||
                                      ra.bottom <= rb.top || rb.bottom <= ra.top);
                return {overlapping, ra, rb};
            }""",
            {"a": v["a"], "b": v["b"]},
        )
        if result.get("missing"):
            return (
                f"assert_no_overlap {v['a']!r} vs {v['b']!r}",
                CheckResult(
                    type=t, passed=False,
                    detail=f"missing element(s): a_found={result['aFound']}, b_found={result['bFound']}",
                ),
            )
        return (
            f"assert_no_overlap {v['a']!r} vs {v['b']!r}",
            CheckResult(
                type=t,
                passed=not result["overlapping"],
                detail="" if not result["overlapping"] else (
                    f"{v['a']!r} overlaps {v['b']!r}"
                ),
            ),
        )

    if t == "assert_style":
        sel = v["selector"]
        prop = v["property"]
        actual = page.evaluate(
            """({sel, prop}) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                return window.getComputedStyle(el).getPropertyValue(prop);
            }""",
            {"sel": sel, "prop": prop},
        )
        if actual is None:
            return (
                f"assert_style {sel!r} {prop!r}",
                CheckResult(type=t, passed=False, detail=f"element {sel!r} not found"),
            )
        actual_norm = actual.strip()
        if "equals" in v:
            expected = v["equals"].strip()
            ok = actual_norm == expected
            label = f"equals {expected!r}"
        elif "not_equals" in v:
            expected = v["not_equals"].strip()
            ok = actual_norm != expected
            label = f"not_equals {expected!r}"
        else:  # contains
            expected = v["contains"].strip()
            ok = expected.lower() in actual_norm.lower()
            label = f"contains {expected!r}"
        return (
            f"assert_style {sel!r} {prop!r} {label}",
            CheckResult(
                type=t,
                passed=ok,
                detail="" if ok else f"{sel!r} {prop!r} is {actual_norm!r}, expected {label}",
            ),
        )

    if t in ("assert_focused", "assert_not_focused"):
        # Use Playwright's locator so selectors like `button:has-text('X')` or
        # `text=X` work — document.activeElement.matches() only understands
        # standard CSS, which is too limiting.
        focused_info = page.evaluate(
            """() => {
                const ae = document.activeElement;
                if (!ae) return '(none)';
                let info = ae.tagName.toLowerCase();
                if (ae.id) info += '#' + ae.id;
                const aria = ae.getAttribute('aria-label');
                if (aria) info += `[aria-label="${aria}"]`;
                const txt = (ae.innerText || '').trim().slice(0, 40);
                if (txt) info += ` "${txt}"`;
                return info;
            }"""
        )
        is_focused = False
        try:
            loc = page.locator(v)
            count = loc.count()
            for i in range(count):
                if loc.nth(i).evaluate("el => el === document.activeElement"):
                    is_focused = True
                    break
        except Exception:
            is_focused = False

        if t == "assert_focused":
            return (
                f"assert_focused {v!r}",
                CheckResult(
                    type=t,
                    passed=is_focused,
                    detail="" if is_focused else f"expected focus on {v!r}, but active element is {focused_info}",
                ),
            )
        else:  # assert_not_focused
            return (
                f"assert_not_focused {v!r}",
                CheckResult(
                    type=t,
                    passed=not is_focused,
                    detail="" if not is_focused else f"{v!r} is unexpectedly focused ({focused_info})",
                ),
            )

    raise RuntimeError(f"unknown step type: {t}")
