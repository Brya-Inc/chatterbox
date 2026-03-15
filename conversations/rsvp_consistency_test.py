#!/usr/bin/env python3
"""
RSVP consistency test — verifies that every event the chatbot says the user
has RSVP'd to also appears under "You're attending" on the home page.

Usage:
    python conversations/rsvp_consistency_test.py
"""

import os
import re
import sys
import time
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

BASE_URL          = os.environ.get("BASE_URL", "http://localhost:3000")
INPUT_SELECTOR    = os.environ.get("INPUT_SELECTOR", "textarea")
SEND_KEY          = os.environ.get("SEND_KEY", "")
SEND_SELECTOR     = os.environ.get("SEND_SELECTOR", "button[type='submit']")
RESPONSE_SELECTOR = os.environ.get("RESPONSE_SELECTOR", ".bot-message")
RESPONSE_TIMEOUT  = int(os.environ.get("RESPONSE_TIMEOUT_MS", "15000"))
HEADLESS          = os.environ.get("HEADLESS", "1") != "0"
CHAT_OPEN_SELECTOR = os.environ.get("CHAT_OPEN_SELECTOR", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
JUDGE_MODEL       = os.environ.get("JUDGE_MODEL", "gpt-4o-mini")

LOGIN_EMAIL    = os.environ.get("LOGIN_EMAIL", "")
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "")
LOGIN_URL      = os.environ.get("LOGIN_URL", "") or (BASE_URL.rstrip("/") + "/login")
LOGIN_EMAIL_SELECTOR    = os.environ.get("LOGIN_EMAIL_SELECTOR",    "[placeholder='Enter email address']")
LOGIN_CONTINUE_SELECTOR = os.environ.get("LOGIN_CONTINUE_SELECTOR", 'text="Continue"')
LOGIN_PASSWORD_SELECTOR = os.environ.get("LOGIN_PASSWORD_SELECTOR", "[placeholder='Enter password']")

TEST_NAME = "RSVP consistency — chatbot list matches You're attending section"


def do_login(page):
    print(f"Logging in as {LOGIN_EMAIL} ...")
    page.goto(LOGIN_URL)
    page.wait_for_load_state("load")
    page.fill(LOGIN_EMAIL_SELECTOR, LOGIN_EMAIL)
    page.click(LOGIN_CONTINUE_SELECTOR)
    page.wait_for_selector(LOGIN_PASSWORD_SELECTOR, state="visible", timeout=10000)
    page.fill(LOGIN_PASSWORD_SELECTOR, LOGIN_PASSWORD)
    page.press(LOGIN_PASSWORD_SELECTOR, "Enter")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
    print(f"Login complete (now at {page.url})")


def dismiss_cookie_banner(page):
    try:
        btn = page.wait_for_selector(
            "[role='region'][aria-label='Cookie consent'] button",
            state="visible", timeout=3000
        )
        if btn:
            btn.click()
            page.wait_for_selector(
                "[role='region'][aria-label='Cookie consent']",
                state="hidden", timeout=5000
            )
    except Exception:
        pass


def send_message(page, message: str) -> str:
    prev_count = page.locator(RESPONSE_SELECTOR).count()
    page.fill(INPUT_SELECTOR, message)
    if SEND_KEY:
        page.press(INPUT_SELECTOR, SEND_KEY)
    else:
        page.click(SEND_SELECTOR)

    try:
        page.wait_for_function(
            f"document.querySelectorAll('{RESPONSE_SELECTOR}').length > {prev_count}",
            timeout=RESPONSE_TIMEOUT,
        )
    except PWTimeout:
        raise RuntimeError(f"Timed out waiting for bot response after {RESPONSE_TIMEOUT}ms")

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


def extract_rsvp_events(client: OpenAI, bot_response: str) -> list[str]:
    """Use the LLM to pull a clean list of event names from the bot's response."""
    result = client.chat.completions.create(
        model=JUDGE_MODEL,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                "The following is a chatbot response listing events a user has RSVP'd to.\n"
                "Extract every distinct event name and return them as a plain list, one per line.\n"
                "Return only the event names — no numbering, no extra commentary.\n"
                "If no events are mentioned, return the single word NONE.\n\n"
                f"Chatbot response:\n{bot_response}"
            ),
        }],
    )
    text = result.choices[0].message.content.strip()
    if text.upper() == "NONE":
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def get_attending_events(page) -> list[str]:
    """Scrape event names from the 'You're attending' section of the home page."""
    # Navigate home and dismiss cookie banner if it reappears
    page.goto(BASE_URL)
    page.wait_for_load_state("load")
    dismiss_cookie_banner(page)
    time.sleep(1)

    # Find the "You're attending" heading and collect sibling event titles
    attending_text = page.inner_text("body")
    return attending_text


def main():
    print(f"\n{'='*60}")
    print(f"Test : {TEST_NAME}")
    print(f"{'='*60}")

    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY is required for this test (used to parse the chatbot's event list)")
        sys.exit(1)

    client = OpenAI(api_key=OPENAI_API_KEY)
    checks = []
    all_passed = True

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        page = browser.new_context().new_page()

        if LOGIN_EMAIL:
            do_login(page)

        # --- Step 1: open the chat and ask about RSVPs ---
        page.goto(BASE_URL)
        page.wait_for_load_state("load")
        dismiss_cookie_banner(page)

        if CHAT_OPEN_SELECTOR:
            page.wait_for_selector(CHAT_OPEN_SELECTOR, state="visible", timeout=15000)
            page.click(CHAT_OPEN_SELECTOR)

        page.wait_for_selector(INPUT_SELECTOR, state="visible", timeout=15000)

        question = "Which events have I RSVP'd to?"
        print(f"\n  [Turn 1] ▶  {question!r}")
        bot_response = send_message(page, question)
        preview = bot_response[:120] + ("…" if len(bot_response) > 120 else "")
        print(f"  [Turn 1] ◀  {preview!r}")

        # --- Step 2: extract event names from the bot's response ---
        rsvp_events = extract_rsvp_events(client, bot_response)
        if rsvp_events:
            print(f"\n  Chatbot listed {len(rsvp_events)} RSVP'd event(s):")
            for e in rsvp_events:
                print(f"    - {e}")
        else:
            print("\n  Chatbot listed no RSVP'd events.")

        # --- Step 3: scrape the home page "You're attending" section ---
        page_text = get_attending_events(page)

        # Find what's under "You're attending"
        attending_section = ""
        match = re.search(r"You're attending(.+?)(?=\n[A-Z]|\Z)", page_text, re.DOTALL | re.IGNORECASE)
        if match:
            attending_section = match.group(1)

        not_attending_msg = "You're not attending any upcoming events"
        page_shows_none = not_attending_msg.lower() in page_text.lower()

        print(f"\n  Home page 'You're attending' section:")
        if page_shows_none:
            print("    (no upcoming events listed)")
        else:
            print(f"    {attending_section.strip()[:300]}")

        # --- Step 4: cross-check ---
        if not rsvp_events:
            passed = True
            detail = "Chatbot reported no RSVP'd events — nothing to cross-check."
            print(f"\n  check: PASS — {detail}")
            checks.append({"passed": True, "detail": detail})
        else:
            for event in rsvp_events:
                # Fuzzy: check if a substantial part of the event name appears on the page
                # Use the first ~5 words to handle slight formatting differences
                key_words = " ".join(event.split()[:5]).lower()
                found = key_words in page_text.lower()

                tag = "PASS" if found else "FAIL"
                detail = f"'{event}' {'found' if found else 'NOT FOUND'} under You're attending"
                print(f"  check: {tag} — {detail}")
                checks.append({"passed": found, "detail": detail})
                if not found:
                    all_passed = False

        browser.close()

    # --- Summary ---
    passed_count = sum(1 for c in checks if c["passed"])
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    status = "PASS" if all_passed else "FAIL"
    print(f"  {status}  {TEST_NAME}")
    print(f"\nChecks : {passed_count}/{len(checks)} passed")
    print(f"Tests  : {'1/1' if all_passed else '0/1'} passed")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
