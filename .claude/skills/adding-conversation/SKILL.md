---
name: adding-conversation
description: Adds a new conversation YAML file to the conversations directory, to be used as a test of the Brya Chatbot website. Use this skill whenever the user requests to add a new conversation, create a test, write a test case, add a scenario, or add anything to the test suite.
---

# Adding conversations

## Workflow

1. Pick the category subdirectory that best fits the test. Create one if nothing fits:
   - `conversations/smoke/` — general discovery / slang / weekend queries
   - `conversations/rsvp/` — RSVP, cancel, attendance
   - `conversations/create/` — event creation flows
2. Pick a short, descriptive, snake_case filename (e.g. `outdoor_events.yaml`, `senior_activities.yaml`).
3. Create the file at `conversations/<subdir>/<filename>.yaml` using the template below.
4. Fill in `name`, `description`, `tags`, the `send` messages, and the matchers.
5. Do **not** set `url` unless the test deliberately targets a non-default host — the default comes from `BASE_URL` in `.env` (`https://www.dev.brya.com`).
6. Add as many turns as the user specifies (default to 2 if not told).

## Template

```yaml
name: "<Descriptive human-readable test name>"
description: "<One sentence describing what this test verifies.>"
tags: [<category>]           # e.g. [smoke], [rsvp], [create] — matches the subdirectory

setup:
  fresh_chat: true            # reload /loggedInHome and reopen chat before this test
  require_events: false       # set true if the test only makes sense when home-page events exist

turns:
  - send: "<First message to send to Sage>"
    expect:
      - judge: "<Natural-language description of what a good response should do>"

  - send: "<Follow-up message>"
    expect:
      - not_contains: "error"
      - judge: "<What a good response should do or contain>"

# Optional — use `final` to assert something about the whole transcript
# (e.g. "the user ended up successfully RSVP'd").
final:
  judge: "<Whole-conversation criterion>"
```

## Schema rules

- `expect` is a **list** of matchers. Each matcher is a single-key mapping.
- Supported matcher types:
  - `judge` — natural-language criterion evaluated by GPT-4o-mini (primary assertion type)
  - `contains` — case-insensitive substring match
  - `not_contains` — case-insensitive substring must be absent
  - `regex` — case-insensitive regex match (Python `re`, DOTALL)
- A turn with no `expect` list just sends the message without asserting anything (useful for setup turns).
- `setup.fresh_chat` forces a reload between tests — use it for any test that needs a clean chat history.
- `setup.require_events` skips the test with a clear reason if the home-page event scraper returns zero events.
- `tags` enable filtering via `python run_tests.py --tag smoke`.

## Placeholders in judge criteria

These are substituted at runtime inside any `judge:` string:

| Placeholder  | Replaced with |
|---|---|
| `{TODAY}`    | Today's date in `YYYY-MM-DD` format |
| `{EVENTS}`   | Newline-separated list of events scraped from the home page (`title \| datetime \| location`) |
| `{MY_EVENTS}`| Newline-separated list of events the user has already RSVP'd to |

## Event carousels / lists

When Sage answers with events, they may appear in the chat as a **carousel** (horizontal scroller) or a **list** (vertical) rather than as plain text. The harness automatically scrapes events added to the chat area during each turn and appends them to the response text the matchers/judge see, so a `judge:` criterion like *"lists at least one event from {EVENTS}"* will still pass if the bot replies with a carousel.

## Example

```yaml
name: "Outdoor events this weekend"
description: "User asks for outdoor weekend activities; Sage should surface matching events."
tags: [smoke]

setup:
  fresh_chat: true
  require_events: true

turns:
  - send: "What outdoor events are happening this weekend?"
    expect:
      - not_contains: "I don't have"
      - judge: |
          The response should list at least one outdoor event scheduled for
          Saturday or Sunday of the current week. Today's date is {TODAY}.

          Cross-reference against the platform events:
          {EVENTS}

          PASS if a cited event appears in the list above and is on the
          weekend. PASS if the bot honestly says there are no matching
          outdoor events and none exist in the list. FAIL if the bot
          fabricates an event or gives a weekday event.
```
