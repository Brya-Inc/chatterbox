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
_INPUT_SELECTORS = [
    "[placeholder='Talk to Sage']",
    "[placeholder='Send message']",
    "[placeholder='Message Sage']",
]

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
        self._active_input = INPUT_SELECTOR  # resolved in open_chat

    # ------------------------------------------------------------------
    # opening the chat
    # ------------------------------------------------------------------

    def open_chat(self) -> None:
        self.page.wait_for_load_state("load")

        # Dismiss overlays in a loop — the page can show several inline prompts
        # sequentially (e.g. "Interested in hosting?" then "Tell me more about…").
        for attempt in range(6):
            self._dismiss_inline_checkin()

            # Already open?
            if self._resolve_input_selector():
                self._wait_for_typing_to_clear()
                return

            # Try known FAB / panel entry points.
            opened = False
            for selector, label in [
                ("button[aria-label='Open chat with Sage']", "floating chat button"),
                ("button:has-text('Continue chat with Sage')", "continue-chat button"),
            ]:
                try:
                    self.page.wait_for_selector(selector, state="visible", timeout=3000)
                    self.page.click(selector)
                    print(f"  [chat] opened via {label}")
                    opened = True
                    break
                except PWTimeout:
                    continue

            if not opened:
                # Chat may already be open but input is behind a suggestion widget.
                if self.page.is_visible("button[aria-label='Close chat']"):
                    print("  [chat] already open (Close chat button visible)")
                    opened = True
                else:
                    for suggestion in ("Something social", "Something active", "Surprise me"):
                        sel = f"button:has-text('{suggestion}')"
                        if self.page.is_visible(sel):
                            self.page.click(sel)
                            print(f"  [chat] opened via suggestion '{suggestion}'")
                            opened = True
                            break

            if opened:
                break

            # Not opened yet — maybe another overlay just appeared; loop and dismiss.
            print(f"  [chat] no entry point on attempt {attempt + 1}, rechecking overlays…")
            time.sleep(1)
        else:
            try:
                self.page.screenshot(path="debug_chat_open_failure.png", timeout=5000)
            except Exception:
                pass
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

        # Wait for any of the known input selectors to become visible.
        for sel in _INPUT_SELECTORS:
            try:
                self.page.wait_for_selector(sel, state="visible", timeout=5000)
                self._active_input = sel
                print(f"  [chat] input found: {sel!r}")
                break
            except PWTimeout:
                continue
        else:
            raise RuntimeError(
                f"Chat opened but no input field became visible.\n"
                f"URL: {self.page.url}"
            )
        self._wait_for_typing_to_clear()

    def _resolve_input_selector(self) -> bool:
        """Check all known input selectors; set self._active_input to the visible one.
        Returns True if found, False otherwise."""
        for sel in _INPUT_SELECTORS:
            if self.page.is_visible(sel):
                self._active_input = sel
                return True
        return False

    def clear_chat_admin(self) -> None:
        """
        Clear chat history via the admin panel:
        Profile bubble → Admin → Start fresh conversation (fallback: Delete all messages).
        Navigates back to loggedInHome and reopens the chat afterwards.

        For non-admin users (no Brya admin access), falls back to a page reload
        + chat reopen instead — the best we can do without admin privileges.
        """
        # Dismiss any overlays that might block the profile button
        self._dismiss_inline_checkin()

        # Open profile menu — use JS to find by text content (handles multi-node buttons)
        clicked = self.page.evaluate("""
            () => {
                const btn = Array.from(document.querySelectorAll('button')).find(
                    b => /\\b(michael|mike|arianna|arianalmark|^[A-Z]$)\\b/i.test(b.innerText || '')
                );
                if (btn) { btn.click(); return true; }
                return false;
            }
        """)
        if not clicked:
            # Profile button not found — fall back to page reload.
            print("  [admin] profile menu button not found, falling back to page reload")
            self._reset_via_reload()
            return

        print("  [admin] opened profile menu")
        time.sleep(0.5)

        # Click Admin — only available for Brya admins. Shorter timeout since we
        # have a graceful fallback for non-admin users.
        try:
            self.page.wait_for_selector("text=Admin", state="visible", timeout=3000)
            self.page.click("text=Admin")
            print("  [admin] clicked Admin")
        except PWTimeout:
            # No admin access (non-Brya user) — close the profile menu and fall back.
            print("  [admin] Admin option not available (non-admin user), falling back to page reload")
            self.page.keyboard.press("Escape")
            time.sleep(0.3)
            self._reset_via_reload()
            return

        # Wait for the admin panel page to fully load before looking for buttons.
        time.sleep(1.5)
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PWTimeout:
            pass

        # Click "Start fresh conversation" (fallback: "Delete all messages")
        cleared = False
        for label in [
            "start fresh conversation",
            "delete all messages",
            "Start fresh",
            "Clear conversation",
            "Reset conversation",
        ]:
            try:
                sel = f'button:has-text("{label}")'
                self.page.wait_for_selector(sel, state="visible", timeout=3000)
                self.page.click(sel)
                print(f"  [admin] clicked '{label}'")
                cleared = True
                break
            except PWTimeout:
                # Also try as plain text selector (not just buttons).
                try:
                    sel = f"text={label}"
                    self.page.wait_for_selector(sel, state="visible", timeout=2000)
                    self.page.click(sel)
                    print(f"  [admin] clicked '{label}' (text)")
                    cleared = True
                    break
                except PWTimeout:
                    continue

        if not cleared:
            # Last resort: fall back to page reload instead of crashing.
            print("  [admin] could not find clear-conversation button, falling back to page reload")
            self._reset_via_reload()
            return

        # Handle any confirmation dialog
        try:
            self.page.wait_for_selector("button:has-text('Confirm')", state="visible", timeout=2000)
            self.page.click("button:has-text('Confirm')")
        except PWTimeout:
            pass

        time.sleep(2)

        # Navigate back to home cleanly
        self._safe_goto_home()
        time.sleep(1)
        print("  [admin] chat cleared and ready")

    def _reset_via_reload(self) -> None:
        """
        Non-admin fallback for clear_chat_admin(): reload the home page and
        reopen the chat. Does NOT wipe server-side chat history (that requires
        admin access), but gives tests a clean UI state to work from.
        """
        self._safe_goto_home()
        time.sleep(1)
        print("  [admin] chat reset via page reload (non-admin — server history not cleared)")

    def _safe_goto_home(self) -> None:
        """Navigate to /loggedInHome, handling redirects that Firefox/WebKit
        treat as errors but Chrome follows silently."""
        try:
            self.page.goto(self.cfg.base_url + "/loggedInHome", wait_until="domcontentloaded")
        except Exception as e:
            err = str(e).lower()
            if "ns_binding_aborted" in err or "interrupted" in err or "aborted" in err:
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
            else:
                raise

    def _dismiss_inline_checkin(self) -> None:
        """Dismiss any overlay/prompt that blocks chat entry points.
        Loops until no more dismissible buttons are found or an input appears."""
        _DISMISS_LABELS = [
            "Not right now", "Maybe", "No",
            "I won't be going", "Not interested", "Skip",
        ]
        for _ in range(5):
            if self._resolve_input_selector():
                return
            dismissed = False
            for label in _DISMISS_LABELS:
                # Use double-quoted selector to handle apostrophes in label text.
                selector = f'button:has-text("{label}")'
                try:
                    self.page.wait_for_selector(selector, state="visible", timeout=1500)
                    self.page.click(selector)
                    print(f"  [chat] dismissed overlay via '{label}'")
                    time.sleep(0.8)
                    dismissed = True
                    break
                except PWTimeout:
                    continue
            if not dismissed:
                break

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

        self.page.fill(self._active_input, message)
        self.page.press(self._active_input, "Enter")

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

    def full_chat_history(self) -> str:
        """Get the full visible chat history — all messages from all conversations
        currently showing in the chat UI. Includes messages from before this test
        if the chat wasn't cleared. Returns formatted text the judge can read."""
        return self.page.evaluate("""
            () => {
                const list = document.querySelector('.cs-message-list');
                if (!list) return '';
                const messages = [];
                for (const el of list.children) {
                    const text = el.innerText.trim();
                    if (!text) continue;
                    const isBot = el.querySelector('.bg-tertiary') !== null
                               || el.classList.contains('bg-tertiary');
                    const role = isBot ? 'Sage' : 'User';
                    messages.push(role + ': ' + text.slice(0, 300));
                }
                // Return last 30 messages to keep it manageable.
                return messages.slice(-30).join('\\n');
            }
        """)

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
