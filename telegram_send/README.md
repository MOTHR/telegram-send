# telegram-send (Hermes Plugin)

Safe outbound Telegram Bot API text sends for Hermes Agent agents.

## Why this exists

Agents sending Telegram messages via raw `curl -d "text=..."` **corrupt text**:
in `application/x-www-form-urlencoded`, a literal `+` is decoded as a **SPACE**
by the server, and bare `%` / `&` break field parsing. A real incident
(2026-09-21): a bot message arrived with

```
Distribution + Vertrieb  ->  Distribution   Vertrieb
Override + Option        ->  Override   Option
```

This plugin registers a single model tool, `telegram_send`, that fixes the whole
bug class:

- **Proper encoding** — the request body is built with `urllib.parse.urlencode`,
  so every character arrives verbatim (`+`, `%`, `&`, quotes, unicode).
- **Round-trip verification** — the response includes the text Telegram echoed
  back; the tool compares it (rstripped — Telegram trims trailing whitespace)
  and fails loudly on any real corruption instead of delivering silently.
- **Profile-scoped tokens** — the bot token is resolved through the active
  profile's secret scope (`agent.secret_scope.get_secret`), never raw
  `os.environ`, which under gateway multiplexing holds the launch profile's
  values and would silently send with the wrong bot.
- **Chunking** — text longer than Telegram's 4096-char limit is split at
  paragraph/newline/space boundaries instead of being rejected.

## Install

From the plugin directory (or after cloning):

```bash
hermes plugins install MOTHR/telegram-send --no-enable
hermes plugins enable telegram-send
```

Or drop it directly into `~/.hermes/plugins/telegram-send/`.

## Configuration

The tool is **opt-in and fails closed**: it surfaces only when a bot token is
resolvable in the active profile's secret scope (`TELEGRAM_BOT_TOKEN` in the
profile `.env` by default) — the same credential the profile's Telegram gateway
already uses. Optional `plugins.entries.telegram-send.settings` keys:

```yaml
plugins:
  enabled:
    - telegram-send
  entries:
    telegram-send:
      settings:
        default_chat_id: "260277354"   # used when the tool is called without chat_id
        token_env: "TELEGRAM_BOT_TOKEN" # env var name in the profile secret scope
        max_message_length: 4096
```

## Tool: `telegram_send`

| Parameter | Required | Description |
|---|---|---|
| `text` | yes | Message text to send verbatim |
| `chat_id` | no | Target chat; falls back to `default_chat_id` |
| `parse_mode` | no | `MarkdownV2` or `HTML`; empty = plain text |
| `disable_notification` | no | Send silently |

Returns `{ok, chat_id, message_ids, chunks, echoed_text, echo_verified}`.

## Tests

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest telegram_send/tests/ -v
```

The registration test loads the plugin through Hermes' real discovery path
against a temp `HERMES_HOME`. The live round-trip test runs against the real
Telegram API when `HERMES_TELEGRAM_SEND_TEST_TOKEN` +
`HERMES_TELEGRAM_SEND_TEST_CHAT` are set.

## License

MIT