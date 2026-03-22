# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Chatterbox is an automated testing harness for web-based chatbots. It uses Playwright to drive a real browser through scripted conversation flows, then optionally uses Claude as an LLM-as-judge to evaluate whether responses meet quality criteria.

## Running tests

```bash
# Run all conversations in ./conversations/
python run_tests.py

# Run a specific conversation file
python run_tests.py conversations/my_test.yaml

# Run with a visible browser (useful for debugging selectors)
HEADLESS=0 python run_tests.py
```

## Environment / configuration

All configuration is via environment variables (see `.env.example`). The critical ones:

| Variable | Purpose | Default |
|---|---|---|
| `BASE_URL` | Target chatbot URL | `http://localhost:3000` |
| `INPUT_SELECTOR` | CSS selector for the chat input | `textarea` |
| `SEND_SELECTOR` | CSS selector for the send button | `button[type='submit']` |
| `RESPONSE_SELECTOR` | CSS selector matching each bot message | `.bot-message` |
| `RESPONSE_TIMEOUT_MS` | How long to wait for a bot reply | `15000` |
| `OPENAI_API_KEY` | Enables LLM-as-judge; omit to skip judging | — |
| `HEADLESS` | Set to `0` to show the browser | `1` |

## Docker

```bash
docker build -t chatterbox .
docker run --env-file .env chatterbox

# Pass a specific conversation file
docker run --env-file .env -v $(pwd)/conversations:/app/conversations chatterbox \
    python run_tests.py conversations/my_test.yaml
```

The image is based on `mcr.microsoft.com/playwright/python` which includes Chromium pre-installed.

## Conversation file format

YAML files in `./conversations/` define test cases. Each file is one named test with multiple turns:

```yaml
name: "Human-readable test name"
url: "https://override-url.com"  # optional, overrides BASE_URL

turns:
  - send: "Message to send"
    expect:
      contains: "substring"   # case-insensitive substring match
      judge: "Natural language description of what a good response looks like"
```

Both `contains` and `judge` are optional and can be combined. A turn with no `expect` key just sends the message without asserting anything.

### Placeholders in judge criteria

Judge criterion strings support these runtime placeholders:

| Placeholder | Replaced with |
|---|---|
| `{TODAY}` | Today's date in `YYYY-MM-DD` format |
| `{EVENTS}` | Newline-separated list of events scraped from the UI (`title \| datetime \| location`) |

## Architecture

Everything lives in `run_tests.py`. The execution flow is:

1. `main()` — resolves which YAML files to run, initializes the OpenAI client and Playwright browser
2. `setup_page()` — handles cookie banner, login, and opens the chat dialog; returns the textarea selector
3. `scrape_events()` — scrolls the home page to trigger lazy-load, then extracts all event cards as `{title, datetime, location}` dicts
4. `run_conversation()` — iterates through turns; accumulates pass/fail state
5. `send_message()` — fills the input, presses Enter, then polls until the last bot message stabilises for 1.5s
6. `judge_response()` — calls the judge model with the user message, bot response, scraped events, and criterion; parses PASS/FAIL from the first line of the reply

A single browser page is reused across all conversation files. Browser state (cookies, localStorage) persists between conversations.

## Adding new check types

New assertion types go in `run_conversation()` alongside the existing `contains` and `judge` blocks. Each check should append to the `checks` list with `{"type": ..., "passed": bool, "detail": ...}` and set `all_passed = False` on failure.
