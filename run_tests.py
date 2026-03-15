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
import yaml
from openai import OpenAI
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# ---------------------------------------------------------------------------
# Config (override via env vars or a .env loader of your choice)
# ---------------------------------------------------------------------------
BASE_URL          = os.environ.get("BASE_URL", "http://localhost:3000")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
JUDGE_MODEL       = os.environ.get("JUDGE_MODEL", "gpt-4o-mini")
INPUT_SELECTOR    = os.environ.get("INPUT_SELECTOR", "textarea")
SEND_SELECTOR     = os.environ.get("SEND_SELECTOR", "button[type='submit']")
SEND_KEY          = os.environ.get("SEND_KEY", "")  # if set, press this key instead of clicking SEND_SELECTOR
RESPONSE_SELECTOR = os.environ.get("RESPONSE_SELECTOR", ".bot-message")
RESPONSE_TIMEOUT  = int(os.environ.get("RESPONSE_TIMEOUT_MS", "15000"))  # ms
HEADLESS          = os.environ.get("HEADLESS", "1") != "0"
CONVERSATIONS_DIR = Path("conversations")

# Login (optional) — if LOGIN_EMAIL is set, the runner logs in once before any tests
LOGIN_EMAIL    = os.environ.get("LOGIN_EMAIL", "")
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "")
LOGIN_URL      = os.environ.get("LOGIN_URL", "")  # defaults to BASE_URL + "/login"
LOGIN_EMAIL_SELECTOR    = os.environ.get("LOGIN_EMAIL_SELECTOR",    "[placeholder='Enter email address']")
LOGIN_CONTINUE_SELECTOR = os.environ.get("LOGIN_CONTINUE_SELECTOR", "text=\"Continue\"")
LOGIN_PASSWORD_SELECTOR = os.environ.get("LOGIN_PASSWORD_SELECTOR", "[placeholder='Enter password']")
LOGIN_SUBMIT_SELECTOR   = os.environ.get("LOGIN_SUBMIT_SELECTOR",   "text=Log in")

# Chat opener (optional) — clicked after page.goto() to reveal the chat UI
CHAT_OPEN_SELECTOR = os.environ.get("CHAT_OPEN_SELECTOR", "")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def do_login(page) -> None:
    """Log in once using LOGIN_EMAIL / LOGIN_PASSWORD env vars."""
    login_url = LOGIN_URL or (BASE_URL.rstrip("/") + "/login")
    print(f"\nLogging in as {LOGIN_EMAIL} ...")
    page.goto(login_url)
    page.wait_for_load_state("load")

    # Step 1: email
    page.fill(LOGIN_EMAIL_SELECTOR, LOGIN_EMAIL)
    page.click(LOGIN_CONTINUE_SELECTOR)
    page.wait_for_load_state("load")

    # Step 2: password
    page.wait_for_selector(LOGIN_PASSWORD_SELECTOR, state="visible", timeout=10000)
    page.fill(LOGIN_PASSWORD_SELECTOR, LOGIN_PASSWORD)
    page.press(LOGIN_PASSWORD_SELECTOR, "Enter")
    # Wait for navigation away from the login page
    page.wait_for_url(lambda url: "/login" not in url, timeout=15000)

    print(f"Login complete (now at {page.url})")


def load_conversations(paths: list[Path]) -> list[dict]:
    conversations = []
    for p in paths:
        with open(p) as f:
            conv = yaml.safe_load(f)
            conv["_file"] = str(p)
            conversations.append(conv)
    return conversations


def judge_response(client: OpenAI, user_msg: str, bot_msg: str, criterion: str) -> tuple[bool, str]:
    """Ask the judge LLM whether a bot response meets the given criterion."""
    result = client.chat.completions.create(
        model=JUDGE_MODEL,
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"You are evaluating a chatbot response.\n\n"
                f"User message:\n{user_msg}\n\n"
                f"Bot response:\n{bot_msg}\n\n"
                f"Evaluation criterion:\n{criterion}\n\n"
                "Does the bot response meet the criterion?\n"
                "Reply with PASS or FAIL on the first line, then a one-sentence explanation."
            ),
        }],
    )
    text = result.choices[0].message.content.strip()
    passed = text.upper().startswith("PASS")
    return passed, text


def send_message(page, message: str) -> str:
    """Type a message, submit it, and return the latest bot response text."""
    # Capture count before sending so we don't race a fast response
    prev_count = page.locator(RESPONSE_SELECTOR).count()

    page.fill(INPUT_SELECTOR, message)
    if SEND_KEY:
        page.press(INPUT_SELECTOR, SEND_KEY)
    else:
        page.click(SEND_SELECTOR)

    # Wait for a new response element to appear
    try:
        page.wait_for_function(
            f"document.querySelectorAll('{RESPONSE_SELECTOR}').length > {prev_count}",
            timeout=RESPONSE_TIMEOUT,
        )
    except PWTimeout:
        raise RuntimeError(
            f"Timed out waiting for a new bot response after {RESPONSE_TIMEOUT}ms.\n"
            f"Check that RESPONSE_SELECTOR='{RESPONSE_SELECTOR}' matches your chatbot's DOM."
        )

    # Poll until the response text stops changing (handles streaming responses)
    deadline = time.time() + RESPONSE_TIMEOUT / 1000
    last_text, stable_since = "", 0.0
    while time.time() < deadline:
        text = page.locator(RESPONSE_SELECTOR).last.inner_text().strip()
        if text != last_text:
            last_text, stable_since = text, time.time()
        elif text and time.time() - stable_since >= 1.5:
            break
        time.sleep(0.3)

    return last_text


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------

def run_conversation(page, conv: dict, client) -> dict:
    name  = conv.get("name", conv["_file"])
    url   = conv.get("url", BASE_URL)
    turns = conv.get("turns", [])

    print(f"\n{'='*60}")
    print(f"Test : {name}")
    print(f"URL  : {url}")
    print(f"{'='*60}")

    page.goto(url)
    page.wait_for_load_state("load")

    # Dismiss cookie consent banner if present
    try:
        cookie_btn = page.wait_for_selector(
            "[role='region'][aria-label='Cookie consent'] button",
            state="visible", timeout=3000
        )
        if cookie_btn:
            cookie_btn.click()
            page.wait_for_selector(
                "[role='region'][aria-label='Cookie consent']",
                state="hidden", timeout=5000
            )
    except Exception:
        pass  # No cookie banner, continue

    if CHAT_OPEN_SELECTOR:
        page.wait_for_selector(CHAT_OPEN_SELECTOR, state="visible", timeout=15000)
        page.click(CHAT_OPEN_SELECTOR)

    page.wait_for_selector(INPUT_SELECTOR, state="visible", timeout=15000)

    results = []
    all_passed = True

    for i, turn in enumerate(turns, 1):
        message = turn["send"]
        expect  = turn.get("expect", {})

        print(f"\n  [Turn {i}] ▶  {message!r}")

        try:
            response = send_message(page, message)
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
                passed, detail = judge_response(client, message, response, criterion)
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

def main():
    # Resolve which conversation files to run
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
        print(f"No conversation files found. Put YAML files in ./{CONVERSATIONS_DIR}/ or pass paths as arguments.")
        sys.exit(1)

    conversations = load_conversations(files)

    # Set up the judge client (optional)
    client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    if not client:
        print("Note: OPENAI_API_KEY not set — LLM-as-judge checks will be skipped.")

    all_results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page    = context.new_page()

        if LOGIN_EMAIL:
            do_login(page)

        for conv in conversations:
            result = run_conversation(page, conv, client)
            all_results.append(result)

        browser.close()

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
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

    sys.exit(0 if not failed_tests else 1)


if __name__ == "__main__":
    main()
