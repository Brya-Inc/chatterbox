# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is

Chatterbox is an automated testing harness for the Brya chatbot (Sage) on `dev.brya.com`. It drives a real browser via Playwright through YAML-defined conversation flows and uses an LLM-as-judge (OpenAI `gpt-4o-mini`) to evaluate whether responses meet natural-language criteria.

The Python package itself lives in `harness/`. The repository is named `chatterbox`; don't confuse the two.

## Running tests

Everything runs inside the Docker container — do not install Python/Playwright on the host.

```bash
# Build
docker build -t chatterbox .

# Full suite against dev.brya.com
docker run --rm --env-file .env chatterbox

# One conversation; mount conversations/ so edits don't require rebuild
docker run --rm --env-file .env \
    -v "$(pwd)/conversations:/app/conversations" \
    chatterbox python run_tests.py conversations/smoke/whats_poppin.yaml

# Tag filter
docker run --rm --env-file .env chatterbox python run_tests.py --tag smoke

# JSON report out to the host
docker run --rm --env-file .env \
    -v "$(pwd)/out:/app/out" \
    chatterbox python run_tests.py --json out/report.json
```

Headed debugging (`HEADLESS=0`) isn't supported in the container — no display. For host-side iteration on selectors, install deps into a venv and run `python run_tests.py` directly; `harness.config` silently skips `python-dotenv` if it's not installed.

## Environment / configuration

All config via env vars — see `.env.example`. The important ones:

| Variable | Purpose | Default |
|---|---|---|
| `BASE_URL` | Target site | `https://www.dev.brya.com` |
| `HEADLESS` | `0` for visible browser | `1` |
| `RESPONSE_TIMEOUT_MS` | Max wait for a bot reply | `20000` |
| `STABLE_MS` | How long the last bubble must be unchanged before accepted | `1500` |
| `RESPONSE_SELECTOR` | CSS selector for bot bubbles | `.cs-message-list .bg-tertiary` |
| `OPENAI_API_KEY` | Enables `judge:` matchers; without it they're skipped | — |
| `JUDGE_MODEL` | OpenAI model | `gpt-4o-mini` |
| `PLAYWRIGHT_LOGIN_URL` + `PLAYWRIGHT_AUTH_KEY` + `LOGIN_EMAIL` | Magic-link login (preferred, matches `brya-web/e2e`) | — |
| `LOGIN_EMAIL` + `LOGIN_PASSWORD` | Fallback interactive login | — |
| `STORAGE_STATE_PATH` | Where Playwright storage state is cached | `.auth/state.json` |

## Conversation file format

YAML files under `conversations/<category>/*.yaml`. Categories are directories: `smoke/`, `rsvp/`, `create/` — add new ones as needed. The schema:

```yaml
name: "Human-readable test name"
description: "One-line summary."
tags: [smoke]                 # enables --tag filtering
url: "https://..."            # optional, overrides BASE_URL

setup:
  fresh_chat: true            # reload /loggedInHome and reopen chat before this test
  require_events: true        # skip with reason if home page has 0 scraped events

turns:
  - send: "Message to Sage"
    expect:
      - judge: "Natural-language criterion"
      - contains: "substring"
      - not_contains: "error"
      - regex: "\\d{4}-\\d{2}-\\d{2}"

final:
  judge: "Whole-transcript criterion"
```

Matcher semantics:
- `expect` is a **list** of matchers. Each is a single-key dict.
- Supported types: `contains`, `not_contains`, `regex` (all case-insensitive), and `judge` (LLM-evaluated).
- `judge:` is evaluated per turn, plus optionally once via `final:` over the whole transcript.
- A turn with no `expect` just sends the message without asserting.

### Placeholders in `judge:` criteria

| Placeholder | Replaced with |
|---|---|
| `{TODAY}` | Today's date, `YYYY-MM-DD` |
| `{EVENTS}` | Home-page events scraped via `scrape_home_events` (`title \| datetime \| location`) |
| `{MY_EVENTS}` | Events the user has RSVP'd to, rescraped between turns |

### Event carousels / lists in chat

When Sage responds with a **carousel** (`SuggestedEventsScroller`, a `ul.overflow-x-auto` of event cards) or an **inline list** (`EventListComponent`, `ol.space-y-3`), `ChatDriver.send` detects the newly-added `a[href*="/event/"]` links inside `.cs-message-list` and appends them to the response text the matchers/judge see. Criteria like *"lists at least one event from {EVENTS}"* keep working whether the bot answers in prose or via UI cards.

## Architecture

The package is `harness/` — each module has a narrow responsibility. Import via `from harness.<mod> import ...` in `run_tests.py`; internal imports use relative form.

1. `run_tests.py` — CLI entry. Argparse (`--tag`, `--json`), loads config, discovers YAMLs, validates schema, runs, reports. Returns an exit code; the `__main__` block skips `sys.exit` when a debugger is attached (so `SystemExit: 1` doesn't surface in IDEs).
2. `harness/config.py` — `load_config()` returns a frozen `Config` dataclass from env vars. `python-dotenv` is optional.
3. `harness/schema.py` — `Conversation`, `Turn`, `Matcher`, `Setup` dataclasses; `load_conversation(path)` + `discover_conversations(root)` with strict validation via `SchemaError`.
4. `harness/auth.py` — `ensure_logged_in(context, page, cfg)` probes `/login`, logs in via magic link or password, and saves the Playwright storage state for reuse.
5. `harness/chat_driver.py` — `ChatDriver.open_chat()` / `send()`. Handles cookie banner, inline check-in widget, multiple chat entry points, streaming-response waiting, "thinking" placeholder bubbles, typing indicator (`.animate-bounce`), and in-chat event card diffing.
6. `harness/scraper.py` — `scrape_home_events()` + `scrape_my_rsvps()` extract event cards from the logged-in home via `a[href*="/event/"]` + `innerText` splitting.
7. `harness/judge.py` — `Judge` wraps the OpenAI client, does placeholder substitution, returns `(passed, reason)`.
8. `harness/matchers.py` — `run_matcher(matcher, user_msg, bot_msg, judge, ctx)` returns `CheckResult(type, passed, detail, skipped)`.
9. `harness/runner.py` — `Runner.run(conversations)` owns the Playwright lifecycle: one browser, one context (loaded from stored auth), one page reused across conversations unless `setup.fresh_chat: true`. Rescrapes `MY_EVENTS` between turns.
10. `harness/report.py` — `print_summary()` with coloured `[PASS]`/`[FAIL]`/`[SKIP]` output; `write_json()` for CI.

## Adding new check types

1. Add the matcher name to `MATCHER_TYPES` in `harness/schema.py`.
2. Handle it in `run_matcher` in `harness/matchers.py`. Return a `CheckResult`.
3. Update the schema section of this file and the `adding-conversation` skill template (`.claude/skills/adding-conversation/SKILL.md`).
4. Add a conversation (or edit `conversations/smoke/_fail_probe.yaml`) that exercises the new matcher.

## Adding new conversations

Use the `adding-conversation` skill. It knows the current schema including categories, `setup`, `tags`, `final`, placeholders, and the carousel/list note.

## Related repos

- `../brya-web` — Next.js frontend. Chat UI lives in `src/components/chat/`. Login fixture we mirror is `e2e/fixtures/login.ts`.
- `../brya-server` — GraphQL/Node backend. Chat mutations route to the LangGraph chatbot.
- `../langgraph_rsvp_chatbot` — the Sage agent itself (find_events, rsvp_to_event, create_event, etc.).
