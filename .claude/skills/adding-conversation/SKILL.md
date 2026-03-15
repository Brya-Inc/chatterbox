---
name: adding-conversation
description: Adds a new conversation YAML file to the conversations directory, to be used as a test of the Brya Chatbot website. Use this skill whenever the user requests to add a new conversation, create a test, write a test case, add a scenario, or add anything to the test suite.
---

# Adding conversations

## Workflow

1. Pick a short, descriptive, snake_case filename based on what the test covers (e.g., `outdoor_events.yaml`, `senior_activities.yaml`)
2. Create the file at `conversations/<filename>.yaml` using the template below
3. Fill in the `name`, the `send` messages, and the `judge` criteria — the `url` is always `https://www.dev.brya.com` and should not be changed
4. Add as many turns as the user specifies (default to 2 if not told)

## Template

```yaml
name: "<Descriptive human-readable test name>"
url: "https://www.dev.brya.com"

turns:
  - send: "<First message to send to the chatbot>"
    expect:
      judge: "<What a good response should do or contain>"

  - send: "<Follow-up message>"
    expect:
      judge: "<What a good response should do or contain>"
```

## Notes

- `judge` is the primary assertion type for Brya tests — write it as a natural language description of what a good response looks like (e.g., "The response should list relevant outdoor events or ask a clarifying question about location or date")
- `contains` (case-insensitive substring match) can also be used alongside `judge` when there's a specific word or phrase the response must include
- A turn with no `expect` key just sends the message without checking anything — useful for setup turns
