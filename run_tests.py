#!/usr/bin/env python3
"""
Chatterbox — automated chatbot testing harness.

Usage:
    python run_tests.py [conversation_file.yaml ...]

If no files are given, all YAML files in ./conversations/ are run.
Configure via environment variables (see .env.example).
"""

import os
import sys
import time
import datetime
import yaml
from openai import OpenAI
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# ---------------------------------------------------------------------------
# Config (override via env vars or .env)
# ---------------------------------------------------------------------------
BASE_URL          = os.environ.get("BASE_URL", "http://localhost:3000")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
JUDGE_MODEL       = os.environ.get("JUDGE_MODEL", "gpt-4o-mini")
RESPONSE_SELECTOR = os.environ.get("RESPONSE_SELECTOR", ".cs-message-list .bg-tertiary")
RESPONSE_TIMEOUT  = int(os.environ.get("RESPONSE_TIMEOUT_MS", "20000"))
HEADLESS          = os.environ.get("HEADLESS", "1") != "0"
CONVERSATIONS_DIR = Path("conversations")

LOGIN_EMAIL    = os.environ.get("LOGIN_EMAIL", "")
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "")

TODAY = datetime.date.today().strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def accept_cookie_banner(page) -> None:
    try:
        btn = page.wait_for_selector(
            "[role='region'][aria-label='Cookie consent'] button:has-text('Accept All')",
            state="visible", timeout=3000,
        )
        if btn:
            btn.click()
            page.wait_for_selector(
                "[role='region'][aria-label='Cookie consent']",
                state="hidden", timeout=5000,
            )
    except Exception:
        pass


def do_login(page) -> None:
    login_url = BASE_URL.rstrip("/") + "/login"
    print(f"\nLogging in as {LOGIN_EMAIL} ...")
    page.goto(login_url)
    page.wait_for_load_state("load")
    # If already authenticated the login page redirects away immediately
    if "/login" not in page.url:
        print(f"Already logged in (at {page.url})")
        return
    page.fill("[placeholder='Enter email address']", LOGIN_EMAIL)
    page.click("text=\"Continue\"")
    page.wait_for_load_state("load")
    page.wait_for_selector("[placeholder='Enter password']", state="visible", timeout=10000)
    page.fill("[placeholder='Enter password']", LOGIN_PASSWORD)
    page.press("[placeholder='Enter password']", "Enter")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
    print(f"Login complete (now at {page.url})")


def setup_page(page) -> str:
    """
    Walk through all pre-chat states: cookie banner → login → open chat dialog.
    Returns the textarea selector to use for sending messages.
    """
    page.wait_for_load_state("load")

    # 1. Cookie consent banner
    accept_cookie_banner(page)

    # 2. Login — always run if credentials are configured; do_login navigates to /login
    #    itself and skips if already authenticated.
    if LOGIN_EMAIL:
        do_login(page)
        page.goto(BASE_URL)
        page.wait_for_load_state("load")
        accept_cookie_banner(page)

    print(f"  [setup] Loaded: {page.url}")

    # 3. Open chat dialog — try several entry points in order of preference
    # a) Already open (textarea visible from a prior interaction)
    # b) Floating sparkle button (old UI)
    # c) "Continue chat with Sage" button (shown after prior inline submission)
    # d) Inline suggestion button (new UI — clicking one opens the full dialog)

    # Pre-step: dismiss inline post-event check-in widget (Yes/No) if present —
    # it blocks access to the main chat entry points until dismissed.
    if not page.is_visible("[placeholder='Talk to Sage']"):
        try:
            page.wait_for_selector("button:has-text('No')", state="visible", timeout=3000)
            page.click("button:has-text('No')")
            print("  [setup] Dismissed inline event check-in (clicked No)")
            time.sleep(1)
        except PWTimeout:
            pass

    textarea_visible = page.is_visible("[placeholder='Talk to Sage']")
    if not textarea_visible:
        opened = False
        for selector, label in [
            ("button[aria-label='Open chat with Sage']", "floating chat button"),
            ("button:has-text('Continue chat with Sage')",  "continue-chat button"),
        ]:
            try:
                page.wait_for_selector(selector, state="visible", timeout=5000)
                page.click(selector)
                opened = True
                print(f"  [setup] Opened chat via {label}")
                break
            except PWTimeout:
                pass

        if not opened:
            # Fall back to an inline suggestion button (new UI)
            for suggestion in ["Something social", "Something active", "Surprise me"]:
                sel = f"button:has-text('{suggestion}')"
                if page.is_visible(sel):
                    page.click(sel)
                    print(f"  [setup] Opened chat via suggestion button '{suggestion}'")
                    opened = True
                    break

        if not opened:
            page.screenshot(path="debug_setup_failure.png")
            buttons = page.evaluate(
                "() => Array.from(document.querySelectorAll('button'))"
                ".map(b => (b.getAttribute('aria-label') || b.innerText.trim().slice(0,40)))"
                ".filter(s => s).join('\\n')"
            )
            raise RuntimeError(
                f"Could not open chat dialog — no known entry point found.\n"
                f"URL: {page.url}\n"
                f"Buttons on page:\n{buttons}\n"
                f"Screenshot saved: debug_setup_failure.png"
            )

    # 4. Wait for the chat textarea inside the dialog
    page.wait_for_selector("[placeholder='Talk to Sage']", state="visible", timeout=15000)

    # 5. Wait for any in-progress bot response to finish streaming before we return.
    #    Clicking the check-in "No" triggers a Sage reply; if it's still streaming when
    #    the caller sends the first test message, prev_count will be unstable.
    try:
        page.wait_for_function(
            "() => !document.querySelector('.animate-bounce')",
            timeout=15000,
        )
    except PWTimeout:
        pass  # typing indicator didn't appear or didn't clear — proceed anyway

    return "[placeholder='Talk to Sage']"


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def scrape_events(page) -> list[dict]:
    """Scrape all visible event cards from the home page."""
    for _ in range(3):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
    page.evaluate("window.scrollTo(0, 0)")

    return page.evaluate("""() => {
        const links = Array.from(document.querySelectorAll('a[href*="/event/"]'));
        return links.map(el => {
            const lines = el.innerText.trim().split('\\n')
                .map(l => l.trim()).filter(l => l && l !== 'No image');
            return { title: lines[0] || '', datetime: lines[1] || '', location: lines[2] || '' };
        }).filter(e => e.title);
    }""")


def scrape_rsvp_events(page) -> list[dict]:
    """Scrape events the user has already RSVP'd to (shown under 'You're attending' or similar)."""
    return page.evaluate("""() => {
        // Find a heading/label containing attending-related text
        const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,div'))
            .filter(el => /you'?re attending|my rsvps|going to/i.test(el.innerText.trim())
                       && el.innerText.trim().length < 80);

        for (const heading of headings) {
            const container = heading.closest('section') || heading.parentElement;
            if (!container) continue;
            const links = Array.from(container.querySelectorAll('a[href*="/event/"]'));
            if (links.length) {
                return links.map(el => {
                    const lines = el.innerText.trim().split('\\n')
                        .map(l => l.trim()).filter(l => l && l !== 'No image');
                    return { title: lines[0] || '', datetime: lines[1] || '', location: lines[2] || '' };
                }).filter(e => e.title);
            }
        }
        return [];
    }""")


def judge_response(client: OpenAI, user_msg: str, bot_msg: str, criterion: str, events=None, rsvp_events=None) -> tuple[bool, str]:
    criterion_filled = criterion.replace("{TODAY}", TODAY)

    events_block = ""
    if events and "{EVENTS}" in criterion_filled:
        lines = [f"- {e['title']} | {e['datetime']} | {e['location']}" for e in events]
        events_block = "Events currently listed on the platform:\n" + "\n".join(lines) + "\n\n"
        criterion_filled = criterion_filled.replace("{EVENTS}", "\n".join(lines))

    if rsvp_events is not None and "{MY_EVENTS}" in criterion_filled:
        lines = [f"- {e['title']} | {e['datetime']} | {e['location']}" for e in rsvp_events]
        rsvp_block = "\n".join(lines) if lines else "(none)"
        events_block += "Events the user has RSVP'd to:\n" + rsvp_block + "\n\n"
        criterion_filled = criterion_filled.replace("{MY_EVENTS}", rsvp_block)

    result = client.chat.completions.create(
        model=JUDGE_MODEL,
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"You are evaluating a chatbot response. Today's date is {TODAY}.\n"
                f"{events_block}"
                f"User message:\n{user_msg}\n\n"
                f"Bot response:\n{bot_msg}\n\n"
                f"Evaluation criterion:\n{criterion_filled}\n\n"
                "Reply with PASS or FAIL on the first line, then a one-sentence explanation."
            ),
        }],
    )
    text = result.choices[0].message.content.strip()
    passed = text.upper().startswith("PASS")
    return passed, text


def _last_response_text(page) -> str:
    """Return the innerText of the last RESPONSE_SELECTOR element via evaluate()."""
    return page.evaluate(
        f"() => {{ const els = document.querySelectorAll('{RESPONSE_SELECTOR}');"
        f" return els.length ? els[els.length - 1].innerText.trim() : ''; }}"
    )


def send_message(page, textarea_selector: str, message: str) -> str:
    """Type a message, submit it, and return the latest bot response text."""
    # Capture count and last text before sending so we can detect a genuinely new reply.
    # Using count increase (new element added) as primary signal avoids false negatives
    # when Sage repeats a prior response verbatim (t !== prev would never fire in that case).
    prev_count = page.evaluate(
        f"() => document.querySelectorAll('{RESPONSE_SELECTOR}').length"
    )
    prev_last_text = _last_response_text(page)

    page.fill(textarea_selector, message)
    page.press(textarea_selector, "Enter")

    # Wait until a new bot message element has been added AND contains non-empty text
    # (the typing-indicator element shares the selector but has empty innerText).
    # Fall back to text-change detection for UIs with a fixed-size message window.
    try:
        page.wait_for_function(
            """([prevCount, prevLast]) => {
                const els = document.querySelectorAll('""" + RESPONSE_SELECTOR + """');
                const count = els.length;
                const t = count ? els[count - 1].innerText.trim() : '';
                return (count > prevCount && t) || (t && t !== prevLast);
            }""",
            arg=[prev_count, prev_last_text],
            timeout=RESPONSE_TIMEOUT,
        )
    except PWTimeout:
        raise RuntimeError(
            f"Timed out waiting for a new bot response after {RESPONSE_TIMEOUT}ms.\n"
            f"Check that RESPONSE_SELECTOR='{RESPONSE_SELECTOR}' matches your chatbot's DOM."
        )

    # Poll via evaluate() until text stabilises for 1.5s (handles streaming).
    deadline = time.time() + RESPONSE_TIMEOUT / 1000
    last_text, stable_since = "", 0.0
    while time.time() < deadline:
        text = _last_response_text(page)
        if text != last_text:
            last_text, stable_since = text, time.time()
        elif text and time.time() - stable_since >= 1.5:
            break
        time.sleep(0.3)

    return last_text


# ---------------------------------------------------------------------------
# Conversation runner
# ---------------------------------------------------------------------------

def run_conversation(page, textarea_selector: str, conv: dict, client, events=None, rsvp_events=None) -> dict:
    name  = conv.get("name", conv["_file"])
    turns = conv.get("turns", [])

    print(f"\n{'='*60}")
    print(f"Test : {name}")
    print(f"{'='*60}")

    results   = []
    all_passed = True

    for i, turn in enumerate(turns, 1):
        message = turn["send"]
        expect  = turn.get("expect", {})

        print(f"\n  [Turn {i}] ▶  {message!r}")

        try:
            response = send_message(page, textarea_selector, message)
        except RuntimeError as e:
            print(f"  [Turn {i}] ERROR: {e}")
            results.append({"turn": i, "send": message, "response": None, "checks": [], "error": str(e)})
            all_passed = False
            continue

        preview = response[:120] + ("…" if len(response) > 120 else "")
        print(f"  [Turn {i}] ◀  {preview!r}")

        checks = []

        # --- contains check ---
        if "contains" in expect:
            needle = expect["contains"]
            passed = needle.lower() in response.lower()
            tag = "PASS" if passed else "FAIL"
            print(f"  [Turn {i}] contains {needle!r}: {tag}")
            checks.append({"type": "contains", "passed": passed, "detail": needle})
            if not passed:
                all_passed = False

        # --- LLM judge check ---
        if "judge" in expect:
            if not client:
                print(f"  [Turn {i}] judge: SKIP (no OPENAI_API_KEY)")
            else:
                criterion = expect["judge"]
                passed, detail = judge_response(client, message, response, criterion, events, rsvp_events)
                tag = "PASS" if passed else "FAIL"
                print(f"  [Turn {i}] judge: {tag} — {detail.splitlines()[1] if len(detail.splitlines()) > 1 else detail}")
                checks.append({"type": "judge", "passed": passed, "detail": detail})
                if not passed:
                    all_passed = False

        results.append({"turn": i, "send": message, "response": response, "checks": checks})

    return {"name": name, "passed": all_passed, "turns": results}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_conversations(paths: list[Path]) -> list[dict]:
    conversations = []
    for p in paths:
        with open(p) as f:
            conv = yaml.safe_load(f)
            conv["_file"] = str(p)
            conversations.append(conv)
    return conversations


def main():
    if len(sys.argv) > 1:
        files = [Path(a) for a in sys.argv[1:]]
        bad = [f for f in files if f.suffix not in (".yaml", ".yml")]
        if bad:
            for f in bad:
                print(f"ERROR: not a YAML file: {f}")
            sys.exit(1)
    else:
        files = sorted(CONVERSATIONS_DIR.glob("*.yaml"))

    if not files:
        print(
            f"No conversation files found. "
            f"Put YAML files in ./{CONVERSATIONS_DIR}/ or pass paths as arguments."
        )
        sys.exit(1)

    conversations = load_conversations(files)

    client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    if not client:
        print("Note: OPENAI_API_KEY not set — LLM-as-judge checks will be skipped.")

    all_results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page    = context.new_page()

        print(f"\nNavigating to {BASE_URL} ...")
        page.goto(BASE_URL)
        textarea_selector = setup_page(page)
        print(f"Chat entry ready (selector: {textarea_selector!r})")

        events = scrape_events(page)
        print(f"Scraped {len(events)} events from the UI")

        rsvp_events = scrape_rsvp_events(page)
        print(f"Scraped {len(rsvp_events)} RSVP'd events from the UI")

        for conv in conversations:
            result = run_conversation(page, textarea_selector, conv, client, events, rsvp_events)
            all_results.append(result)

        browser.close()

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    total_checks  = 0
    passed_checks = 0
    failed_tests  = []

    for conv in all_results:
        status = "PASS" if conv["passed"] else "FAIL"
        print(f"  {status}  {conv['name']}")
        if not conv["passed"]:
            failed_tests.append(conv["name"])
        for turn in conv["turns"]:
            for check in turn.get("checks", []):
                total_checks += 1
                if check["passed"]:
                    passed_checks += 1

    print(f"\nChecks : {passed_checks}/{total_checks} passed")
    print(f"Tests  : {len(all_results) - len(failed_tests)}/{len(all_results)} passed")

    sys.exit(0)


if __name__ == "__main__":
    main()
