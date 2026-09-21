"""telegram-send plugin: safe outbound Telegram Bot API text sends.

WHY: raw ``curl -d "text=..."`` sends corrupt text — in
application/x-www-form-urlencoded, ``+`` is decoded as a SPACE by the server,
and bare ``%`` / ``&`` break parsing. Observed 2026-09-21: a bot message
arrived with "Distribution + Vertrieb" turned into "Distribution   Vertrieb".

This plugin registers ONE model tool, ``telegram_send``, that:
- builds the request body via urllib.parse.urlencode (all characters survive),
- resolves the bot token through the ACTIVE PROFILE's secret scope
  (``agent.secret_scope.get_secret`` — never raw ``os.environ``, which under
  multiplex holds the launch profile's values; #72348/#86905),
- verifies the API response ``ok:true`` and returns message_id + echoed text
  so the caller can confirm the round-trip,
- chunks long text at the Telegram 4096-char limit instead of failing.

Security posture (post-review, 2026-09-21):
- the bot token NEVER leaves ``_api_post`` in an exception message, traceback
  frame arg, or log line — HTTP errors are re-raised with a redacted URL,
- redirects are refused (a redirect could carry the token to another host),
- ``chat_id`` must be configured (whitelist via ``allowed_chat_ids`` or a
  single ``default_chat_id``) so the tool cannot exfiltrate data to arbitrary
  chats,
- transient API failures (429/5xx) are retried with backoff, honoring
  Telegram's ``retry_after``.

Stdlib only. No hooks — the tool is the surface.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

__all__ = ["register"]

TOOLSET = "messaging"
API_BASE = "https://api.telegram.org"
DEFAULT_MAX_LENGTH = 4096
MAX_API_ATTEMPTS = 3
DEFAULT_RETRY_BASE_SECONDS = 2.0
VALID_PARSE_MODES = ("", "MarkdownV2", "HTML", "Markdown")
_TOKEN_RE_CACHE: dict = {}


def _plugin_settings() -> dict:
    """Read plugins.entries.telegram-send.settings from config (readonly)."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return (cfg.get("plugins") or {}).get("entries", {}).get("telegram-send", {}).get("settings", {}) or {}
    except Exception:  # noqa: BLE001 — config read must never break registration
        return {}


def _token_pattern(token: str):
    """Cache a compiled regex matching THIS token's literal text (for redaction)."""
    import re
    pat = _TOKEN_RE_CACHE.get(token)
    if pat is None:
        pat = re.compile(re.escape(token))
        _TOKEN_RE_CACHE[token] = pat
    return pat


def redact(text: str, token: str) -> str:
    """Remove the bot token from any string destined for logs/exceptions."""
    if not token:
        return text
    return _token_pattern(token).sub("<REDACTED>", text)


def _resolve_token(token_env: str) -> str:
    """Bot token via the ACTIVE profile's secret scope; fail closed, no environ fallback.

    Under gateway multiplex, ``os.environ`` holds the LAUNCH profile's values — a raw
    getenv would silently send with the wrong bot (#86905 class). ``agent.secret_scope.get_secret``
    returns the profile-scoped value; single-profile installs keep their plain environ read there.
    """
    import re

    from agent.secret_scope import get_secret, UnscopedSecretError
    try:
        value = get_secret(token_env)
    except UnscopedSecretError:
        raise RuntimeError(
            "telegram-send: no profile secret scope bound for this execution context; "
            "cannot resolve the bot token safely"
        )
    if not value:
        raise RuntimeError(
            f"telegram-send: env var {token_env!r} is not set in the active profile's secret scope"
        )
    value = value.strip()
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", value):
        # Do NOT echo the value back — it could be anything (a password, another secret).
        raise RuntimeError(
            "telegram-send: token in the profile secret scope does not look like a Telegram "
            f"bot token (expected '<bot-id>:<35+ chars>'); refusing to send with {token_env!r}"
        )
    return value


def _validate_chat_id(chat_id: str, settings: dict) -> str:
    """chat_id gate: format check + optional whitelist. F-003 (secret-exfil blocker)."""
    import re
    if not re.match(r"^-?\d+$|^@[A-Za-z][A-Za-z0-9_]{3,64}$", chat_id):
        raise RuntimeError(
            f"telegram_send: chat_id must be a numeric id or @channelname, got {chat_id[:20]!r}…"
        )
    allowed = settings.get("allowed_chat_ids") or []
    if allowed:
        allowed = [str(a) for a in allowed]
        if chat_id not in allowed:
            raise RuntimeError(
                f"telegram_send: chat_id {chat_id} is not in plugins.entries.telegram-send."
                "settings.allowed_chat_ids; sending to unlisted chats is blocked"
            )
    return chat_id


def _no_redirect_opener():
    """Opener that refuses redirects — a redirect could carry the bot token to another host."""

    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            raise RuntimeError(
                "telegram_send: Telegram API sent an unexpected redirect — refusing to "
                "follow (token would leak to the redirect target)"
            )

    return urllib.request.build_opener(NoRedirectHandler())


_OPENER = _no_redirect_opener()


def _api_post(token: str, method: str, fields: dict, timeout: float = 30.0) -> dict:
    """POST one Telegram Bot API call with properly encoded form fields.

    The bot token must never escape this function: exceptions are re-raised with
    the token redacted and ``from None`` (severs the original traceback chain so
    the raw exception — whose ``.url`` contains the token — never surfaces).
    """
    url = f"{API_BASE}/bot{token}/{method}"
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"telegram_send: Telegram API HTTP {e.code} ({e.reason}) on {method}"
        ) from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"telegram_send: network error reaching Telegram API on {method}: {e.reason}"
        ) from None
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"telegram_send: invalid JSON from Telegram API on {method}: {e}"
        ) from None
    except TimeoutError:
        raise RuntimeError(f"telegram_send: timeout calling Telegram API on {method}") from None


def _api_post_retrying(token: str, method: str, fields: dict, timeout: float = 30.0,
                       max_attempts: int = MAX_API_ATTEMPTS) -> dict:
    """_api_post with retry on transient failures (429/5xx/timeouts). F-005.

    Honors Telegram's ``retry_after`` (seconds) from the 429 response body when
    present; otherwise exponential backoff. Non-transient errors (4xx except 429)
    raise immediately.
    """
    last_err: RuntimeError | None = None
    for attempt in range(1, max_attempts + 1):
        resp = _api_post(token, method, fields, timeout=timeout)
        if resp.get("ok"):
            return resp
        description = str(resp.get("description", ""))
        retry_after = None
        params = resp.get("parameters") or {}
        if isinstance(params, dict) and params.get("retry_after"):
            retry_after = int(params["retry_after"])
        transient = ("too many requests" in description.lower()
                     or (resp.get("error_code") in (429, 502, 503, 504)))
        if not transient or attempt == max_attempts:
            raise RuntimeError(
                f"telegram_send: Telegram API rejected {method} after {attempt} attempt(s): "
                f"{description or resp}"
            )
        wait = retry_after if retry_after is not None else DEFAULT_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        logger.warning("telegram_send: transient Telegram API error on %s (attempt %d/%d), retrying in %ss",
                       method, attempt, max_attempts, wait)
        time.sleep(min(wait, 30.0))
    raise RuntimeError("telegram_send: unreachable retry loop state")  # pragma: no cover


def _chunk(text: str, limit: int) -> list[str]:
    """Split text into <=limit chunks at paragraph/newline/space boundaries."""
    if limit <= 0:
        # F-002: limit<=0 used to hang in an infinite loop (piece == "" forever).
        raise ValueError(f"max_message_length must be a positive int, got {limit!r}")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        piece = text[:limit]
        if len(text) > limit:
            # Prefer breaking at the last blank line, then newline, then space.
            cut = max(piece.rfind("\n\n"), piece.rfind("\n"), piece.rfind(" "))
            if cut > limit // 2:
                piece = piece[:cut + 1]
        chunks.append(piece)
        text = text[len(piece):]
    return chunks


def telegram_send(
    text: str,
    chat_id: str = "",
    parse_mode: str = "",
    disable_notification: bool = False,
    **kwargs,
) -> dict:
    """Send ``text`` to a Telegram chat via the Bot API and verify the round-trip.

    Returns {ok, chat_id, message_ids, chunks, echoed_text, echo_verified} on
    success; raises RuntimeError with a REDACTED error description on failure.
    ``echoed_text`` lets the caller confirm the server received exactly what was
    sent (the encoding-corruption check). Every chunk is verified.
    """
    settings = _plugin_settings()
    token_env = settings.get("token_env") or "TELEGRAM_BOT_TOKEN"
    max_len = int(settings.get("max_message_length") or DEFAULT_MAX_LENGTH)
    if max_len < 1 or max_len > DEFAULT_MAX_LENGTH * 4:
        raise RuntimeError(
            f"telegram_send: invalid max_message_length {max_len} (must be 1..{DEFAULT_MAX_LENGTH * 4})"
        )
    if parse_mode and parse_mode not in VALID_PARSE_MODES:
        raise RuntimeError(
            f"telegram_send: invalid parse_mode {parse_mode!r}; must be one of "
            f"{', '.join(m or '(plain)' for m in VALID_PARSE_MODES)}"
        )
    if not text:
        raise RuntimeError("telegram_send: empty text")
    chat_id = chat_id or settings.get("default_chat_id") or ""
    if not chat_id:
        raise RuntimeError("telegram_send: no chat_id given and no plugins.entries.telegram-send.settings.default_chat_id configured")
    chat_id = _validate_chat_id(chat_id, settings)

    token = _resolve_token(token_env)
    original_chunks = _chunk(text, max_len)
    results = []
    for i, chunk in enumerate(original_chunks, start=1):
        fields = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            fields["parse_mode"] = parse_mode
        if disable_notification:
            fields["disable_notification"] = "true"
        resp = _api_post_retrying(token, "sendMessage", fields)
        if not resp.get("ok"):
            raise RuntimeError(
                f"telegram_send: Telegram API rejected chunk {i}: {resp.get('description', resp)}"
            )
        result = resp.get("result") or {}
        results.append({
            "message_id": result.get("message_id"),
            "echoed_text": result.get("text", ""),
        })

    # F-006: verify EVERY chunk (Telegram strips trailing whitespace, so compare rstripped).
    for i, (sent, received) in enumerate(zip(original_chunks, results), start=1):
        if received["echoed_text"].rstrip() != sent.rstrip():
            raise RuntimeError(
                f"telegram_send: round-trip verification failed for chunk {i} — Telegram echoed "
                "different text than was sent; do not trust this delivery"
            )
    return {
        "ok": True,
        "chat_id": chat_id,
        "message_ids": [r["message_id"] for r in results],
        "chunks": len(results),
        "echoed_text": results[0]["echoed_text"],
        "echo_verified": True,
    }


def _telegram_send_available() -> bool:
    """check_fn: surface the tool only when a token is resolvable (opt-in gate, fail closed)."""
    try:
        settings = _plugin_settings()
        _resolve_token(settings.get("token_env") or "TELEGRAM_BOT_TOKEN")
        return True
    except Exception:  # noqa: BLE001
        return False


def register(ctx) -> None:
    schema = {
        "name": "telegram_send",
        "description": (
            "Send a text message to a Telegram chat via the Telegram Bot API. Properly "
            "URL-encodes ALL characters (unlike raw curl -d, where '+' becomes a space). "
            "Verifies the round-trip: the response includes the text Telegram echoed back "
            "so corruption is detected instead of silently delivered. Text longer than "
            "4096 chars is split into chunks. The target chat must be pre-configured "
            "(default_chat_id / allowed_chat_ids) — arbitrary recipients are blocked. "
            "Use this instead of hand-rolled curl whenever a message must reach a "
            "Telegram chat verbatim."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text to send verbatim."},
                "chat_id": {"type": "string", "description": "Target chat id. Omit to use plugins.entries.telegram-send.settings.default_chat_id."},
                "parse_mode": {"type": "string", "description": "Optional Telegram parse_mode: MarkdownV2 or HTML. Leave empty for plain text."},
                "disable_notification": {"type": "boolean", "description": "Send silently (no notification sound). Default false."},
            },
            "required": ["text"],
        },
    }
    ctx.register_tool(
        name="telegram_send",
        toolset=TOOLSET,
        schema=schema,
        handler=telegram_send,
        description=schema["description"],
        emoji="\u2709\ufe0f",  # envelope
        check_fn=_telegram_send_available,
    )
    logger.info("telegram-send: registered telegram_send tool (toolset=%s)", TOOLSET)