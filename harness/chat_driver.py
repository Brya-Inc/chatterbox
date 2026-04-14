"""
Drives the Sage chat UI on dev.brya.com.

Selectors (sourced from brya-web/src/components/chat/):
  - Input:          [placeholder="Talk to Sage"]
  - Bot bubbles:    .cs-message-list .bg-tertiary
  - Typing dots:    .animate-bounce
  - Open buttons:   aria-label='Open chat with Sage' / "Continue chat with Sage"
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, TimeoutError as PWTimeout

from .config import Config

INPUT_SELECTOR = "[placeholder='Talk to Sage']"

# Short placeholder bubbles Sage emits while it's still working. These must not
# be treated as the final response — keep polling until something else appears.
_THINKING_RE = re.compile(
    r"^\s*(one second|thinking|let me (think|check|see|look)|just a (moment|sec|second)|"
    r"hold on|working on it|checking|looking (into|that) up|give me a (moment|sec))"
    r"[\s\.\,…!]*$",
    re.IGNORECASE,
)


def _is_thinking(text: str) -> bool:
    if not text or len(text) > 80:
        return False
    return bool(_THINKING_RE.match(text))


class ChatDriver:
    def __init__(self, page: Page, cfg: Config):
        self.page = page
        self.cfg = cfg
        self.response_selector = cfg.response_selector

    # ------------------------------------------------------------------
    # opening the chat
    # ------------------------------------------------------------------

    def open_chat(self) -> None:
        self.page.wait_for_load_state("load")
        self._dismiss_inline_checkin()

        if self.page.is_visible(INPUT_SELECTOR):
            self._wait_for_typing_to_clear()
            return

        for selector, label in [
            ("button[aria-label='Open chat with Sage']", "floating chat button"),
            ("button:has-text('Continue chat with Sage')", "continue-chat button"),
        ]:
            try:
                self.page.wait_for_selector(selector, state="visible", timeout=5000)
                self.page.click(selector)
                print(f"  [chat] opened via {label}")
                break
            except PWTimeout:
                continue
        else:
            for suggestion in ("Something social", "Something active", "Surprise me"):
                sel = f"button:has-text('{suggestion}')"
                if self.page.is_visible(sel):
                    self.page.click(sel)
                    print(f"  [chat] opened via suggestion '{suggestion}'")
                    break
            else:
                self.page.screenshot(path="debug_chat_open_failure.png")
                buttons = self.page.evaluate(
                    "() => Array.from(document.querySelectorAll('button'))"
                    ".map(b => (b.getAttribute('aria-label') || b.innerText.trim().slice(0,60)))"
                    ".filter(s => s).join(' | ')"
                )
                raise RuntimeError(
                    f"Could not open chat dialog — no known entry point found.\n"
                    f"URL: {self.page.url}\n"
                    f"Buttons: {buttons[:1000]}\n"
                    f"Screenshot: debug_chat_open_failure.png"
                )

        self.page.wait_for_selector(INPUT_SELECTOR, state="visible", timeout=15000)
        self._wait_for_typing_to_clear()

    def _dismiss_inline_checkin(self) -> None:
        """The post-event inline check-in widget can block chat entry points."""
        if self.page.is_visible(INPUT_SELECTOR):
            return
        try:
            self.page.wait_for_selector("button:has-text('No')", state="visible", timeout=3000)
            self.page.click("button:has-text('No')")
            print("  [chat] dismissed inline check-in")
            time.sleep(1)
        except PWTimeout:
            pass

    def _wait_for_typing_to_clear(self) -> None:
        try:
            self.page.wait_for_function(
                "() => !document.querySelector('.animate-bounce')",
                timeout=15000,
            )
        except PWTimeout:
            pass

    # ------------------------------------------------------------------
    # sending + reading
    # ------------------------------------------------------------------

    def send(self, message: str) -> str:
        prev_count = self.page.evaluate(
            f"() => document.querySelectorAll('{self.response_selector}').length"
        )
        prev_last = self._last_response_text()
        prev_event_hrefs = self._chat_event_hrefs()

        self.page.fill(INPUT_SELECTOR, message)
        self.page.press(INPUT_SELECTOR, "Enter")

        try:
            self.page.wait_for_function(
                """([prevCount, prevLast, sel]) => {
                    const els = document.querySelectorAll(sel);
                    const count = els.length;
                    const t = count ? els[count - 1].innerText.trim() : '';
                    return (count > prevCount && t) || (t && t !== prevLast);
                }""",
                arg=[prev_count, prev_last, self.response_selector],
                timeout=self.cfg.response_timeout_ms,
            )
        except PWTimeout:
            raise RuntimeError(
                f"Timed out waiting for a new bot response after {self.cfg.response_timeout_ms}ms. "
                f"Selector: {self.response_selector}"
            )

        text = self._await_stable_text(prev_count)
        new_events = self._new_chat_events(prev_event_hrefs)
        if new_events:
            lines = "\n".join(
                f"- {e['title']} | {e['datetime']} | {e['location']}".rstrip(" |")
                for e in new_events
            )
            text = f"{text}\n\nEvents shown in chat (carousel or list):\n{lines}"
        return text

    def _await_stable_text(self, prev_count: int) -> str:
        """
        Poll the last bot bubble until its text has been stable for stable_ms AND
        it is not a "thinking" placeholder and no typing indicator is showing.
        If the latest bubble is a thinking placeholder, keep waiting for either
        (a) a newer bubble to replace it or (b) its text to become real content.
        """
        deadline = time.time() + self.cfg.response_timeout_ms / 1000
        stable_for = self.cfg.stable_ms / 1000
        last_text = ""
        stable_since = 0.0
        while time.time() < deadline:
            count, text = self._last_response_snapshot()

            # Thinking placeholder — reset and keep waiting for the real reply.
            if _is_thinking(text):
                last_text, stable_since = text, 0.0
                time.sleep(0.3)
                continue

            # Typing indicator still bouncing — bot is mid-stream.
            if self._typing_indicator_visible():
                last_text, stable_since = text, time.time()
                time.sleep(0.3)
                continue

            if text != last_text:
                last_text, stable_since = text, time.time()
            elif text and count > prev_count and time.time() - stable_since >= stable_for:
                return text
            time.sleep(0.3)
        return last_text

    def _last_response_snapshot(self) -> tuple[int, str]:
        return self.page.evaluate(
            f"() => {{ const els = document.querySelectorAll('{self.response_selector}');"
            f" const t = els.length ? els[els.length - 1].innerText.trim() : '';"
            f" return [els.length, t]; }}"
        )

    def _typing_indicator_visible(self) -> bool:
        return self.page.evaluate(
            "() => !!document.querySelector('.animate-bounce')"
        )

    def _last_response_text(self) -> str:
        return self.page.evaluate(
            f"() => {{ const els = document.querySelectorAll('{self.response_selector}');"
            f" return els.length ? els[els.length - 1].innerText.trim() : ''; }}"
        )

    def transcript(self) -> list[str]:
        """All bot messages currently on screen, in order."""
        return self.page.evaluate(
            f"() => Array.from(document.querySelectorAll('{self.response_selector}'))"
            f".map(el => el.innerText.trim()).filter(Boolean)"
        )

    # ------------------------------------------------------------------
    # in-chat event cards (carousel or list)
    # ------------------------------------------------------------------

    def _chat_event_hrefs(self) -> list[str]:
        """Ordered list of event hrefs currently visible in the chat message list."""
        return self.page.evaluate(
            """() => {
                const root = document.querySelector('.cs-message-list');
                if (!root) return [];
                return Array.from(root.querySelectorAll('a[href*="/event/"]'))
                    .map(a => a.getAttribute('href') || '');
            }"""
        )

    def _new_chat_events(self, prev_hrefs: list[str]) -> list[dict]:
        """
        Events rendered in the chat message area that weren't there before the
        last send. Covers both the carousel (SuggestedEventsScroller) and the
        inline list (EventListComponent) since both render each event as an
        `<a href="/event/...">` link inside `.cs-message-list`.
        """
        prev_set = set(prev_hrefs)
        return self.page.evaluate(
            """(prev) => {
                const root = document.querySelector('.cs-message-list');
                if (!root) return [];
                const seen = new Set();
                const out = [];
                for (const el of root.querySelectorAll('a[href*="/event/"]')) {
                    const href = el.getAttribute('href') || '';
                    if (!href || prev.includes(href) || seen.has(href)) continue;
                    seen.add(href);
                    const lines = el.innerText.trim().split('\\n')
                        .map(l => l.trim())
                        .filter(l => l && l !== 'No image');
                    out.push({
                        href,
                        title: lines[0] || '',
                        datetime: lines[1] || '',
                        location: lines[2] || '',
                    });
                }
                return out.filter(e => e.title);
            }""",
            list(prev_set),
        )
