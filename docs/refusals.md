# Refusal detection

When a model declines a request, the answer gets an amber warning in its action row, after the
fork (share) button. Hovering it says what was declined and how Mologate knows.

| Warning         | Meaning                                                              |
| --------------- | -------------------------------------------------------------------- |
| Declined        | The model refused the request                                        |
| Partly declined | It refused, then helped with something anyway (an alternative, part) |
| Filtered        | The provider's content filter blocked the answer or stopped it early |

The check runs once the whole response has finished, after any tool calls, and is
saved on the message (`refusal`), so the warning stays after a reload and in shared chats.
Provider errors keep their own message under the answer, as before; a refusal among them gets
the warning too.

## How a refusal is recognized

`backend/open_webui/utils/refusals.py`, from two kinds of evidence:

- **The provider says so.** OpenAI's `refusal` field (Chat Completions) and refusal parts
  (Responses), a `content_filter`, `refusal` or safety finish (also OpenRouter's
  `native_finish_reason`, Gemini's `SAFETY` and similar), a moderation error, or Mologate's own
  provider-account notes. Refusal text from these fields is shown as the answer; before, a
  refusal from a regular OpenAI connection could leave the answer empty. Provider accounts turn
  refusals into text themselves and mark the reply with `"mologate": {"refusal": true}`, which
  API clients ignore.
- **The answer says so.** Its opening (the first four sentences, up to 600 characters) declines
  the request: "I can't help with that", "I must decline", "against my guidelines", "Извините, но
  я не могу помочь", "抱歉，我无法…", and the same in Ukrainian, Spanish, Portuguese, French,
  German, Italian, Turkish, Japanese and Korean. More than 700 characters after the declining
  sentence makes it "Partly declined".

The wording check is built to stay quiet when in doubt:

- only the final answer counts, not reasoning, code blocks, inline code, block quotes or quoted
  phrases ("the bot should reply 'I can't help with that'");
- a limit of the model's reach is not a refusal: "I can't access the internet", "I'm unable to
  provide real-time prices", "I can't help with that until you share the log", "I can't open the
  file because the file is missing";
- a declining phrase later in a long answer is ignored.

It is a heuristic, and the tooltip says so ("Detected from the answer's wording, so it may be
wrong"); a provider signal says "Reported by the provider". `MOLOGATE_REFUSAL_DETECTION=false`
turns detection off.

## Tests

`backend/open_webui/test/test_refusals.py`: refusals in eleven languages, look-alikes that must
not count (limits of reach, agent reports, quotes, code, reasoning), partial refusals, provider
signals in Chat Completions and Responses streams, and outright failures.
`test/limits/test_provider_accounts.py` checks that provider accounts mark the refusals they turn
into text.
