# telegram-send

![Version](https://img.shields.io/badge/version-1.1.0-blue) ![License](https://img.shields.io/badge/license-MIT-green)

**Safe outbound Telegram Bot API text sends for [Hermes Agent](https://github.com/nousresearch/hermes-agent) agents** — proper URL-encoding for every character, round-trip delivery verification, secret-store token handling, and chunking for long messages. Stdlib only.

> A hardened, confirmation-gated mailbox for agents — instead of every agent
> building its own error-prone `curl` postal route.

## Why

Agents sending Telegram messages via raw `curl -d "text=..."` **corrupt text**. In `application/x-www-form-urlencoded`, a literal `+` is decoded as a **space** by the server, and bare `%` / `&` break field parsing. A real incident (2026-09-21):

```
sent:  Distribution + Vertrieb
arrived:  Distribution   Vertrieb
```

This plugin fixes the whole bug class with a single model tool, `telegram_send`.

## Features

- **Verbatim delivery** — request body built with `urllib.parse.urlencode`; every character (`+`, `%`, `&`, quotes, unicode) arrives as sent.
- **Round-trip verification** — the tool compares the text Telegram echoes back and fails loudly on any real corruption instead of delivering silently.
- **Secret-store tokens** — the bot token resolves through the active profile's secret scope (`agent.secret_scope.get_secret`), never raw `os.environ` (which under gateway multiplexing would hold the launch profile's values and silently send with the wrong bot). Tokens never appear in logs or error messages.
- **Recipient allowlist** — optionally restrict sending to operator-configured chats only.
- **Chunking** — texts over Telegram's 4096-char limit are split at paragraph/newline/space boundaries instead of being rejected; transient rate-limits are retried automatically.
- **Fail closed** — the tool only surfaces when a token is resolvable; no token, no tool.

## Install

```bash
hermes plugins install MOTHR/telegram-send --no-enable
hermes plugins enable telegram-send
```

Or drop it directly into `~/.hermes/plugins/telegram-send/`.

## Configuration

Opt-in via token presence. Optional keys under `plugins.entries.telegram-send.settings`:

```yaml
plugins:
  enabled:
    - telegram-send
  entries:
    telegram-send:
      settings:
        default_chat_id: "YOUR_CHAT_ID"     # used when the tool is called without chat_id
        allowed_chat_ids: ["YOUR_CHAT_ID"]  # optional whitelist; when set, ONLY these chats are sendable
        token_env: "TELEGRAM_BOT_TOKEN"     # env var name in the profile secret scope
        max_message_length: 4096
```

## Tool: `telegram_send`

| Parameter | Required | Description |
|---|---|---|
| `text` | yes | Message text to send verbatim |
| `chat_id` | no | Target chat; falls back to `default_chat_id` |
| `parse_mode` | no | `MarkdownV2`, `HTML`, or `Markdown`; empty = plain text |
| `disable_notification` | no | Send silently |

Returns `{ok, chat_id, message_ids, chunks, echoed_text, echo_verified}`.

## Part of the Hermes security stack

One of three complementary layers — ingress → content → egress:

1. **Transport — this plugin.** *"Does what was sent actually arrive?"* Correct encoding, round-trip verification, secret-store tokens, allowlisted recipients.
2. **Outbound content — Privacy Shield.** *"May this content leave at all?"* Masks tokens/PII in outgoing text before it hits the channel.
3. **Inbound content — Injection Shield.** *"Is incoming content trying to manipulate the agent?"* Filters prompt injections arriving through messaging channels.

A send made through `telegram_send` is still subject to Privacy Shield masking — by design, not by accident.

## Tests

```bash
python -m pytest telegram_send/tests/ -v
```

The registration test loads the plugin through Hermes' real discovery path against a temp `HERMES_HOME`. The live round-trip test runs against the real Telegram API when `HERMES_TELEGRAM_SEND_TEST_TOKEN` + `HERMES_TELEGRAM_SEND_TEST_CHAT` are set.

## Security

Security-hardened in v1.1.0 through structured multi-reviewer adversarial testing (encoding edge cases, token-exposure scenarios, recipient-allowlist bypass attempts). If you find a vulnerability, please open a private security advisory rather than a public issue.

## License

MIT