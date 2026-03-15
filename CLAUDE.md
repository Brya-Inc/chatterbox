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
| `ANTHROPIC_API_KEY` | Enables LLM-as-judge; omit to skip judging | — |
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

## Architecture

Everything lives in `run_tests.py`. The execution flow is:

1. `main()` — resolves which YAML files to run, initializes the Anthropic client and Playwright browser
2. `run_conversation()` — navigates to the URL and iterates through turns; accumulates pass/fail state
3. `send_message()` — fills the input, clicks send, then waits for the bot-message count in the DOM to increase before returning the new response text
4. `judge_response()` — calls `claude-sonnet-4-6` with the user message, bot response, and criterion; parses PASS/FAIL from the first line of the reply

A single browser page is reused across all conversation files. Each conversation navigates to its URL fresh (via `page.goto`), but browser state (cookies, localStorage) persists between conversations unless the context is reset.

## Adding new check types

New assertion types go in `run_conversation()` alongside the existing `contains` and `judge` blocks. Each check should append to the `checks` list with `{"type": ..., "passed": bool, "detail": ...}` and set `all_passed = False` on failure.
