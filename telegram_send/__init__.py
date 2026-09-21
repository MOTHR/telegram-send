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

Stdlib only. No hooks — the tool is the surface.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

__all__ = ["register"]

TOOLSET = "messaging"
API_BASE = "https://api.telegram.org"
DEFAULT_MAX_LENGTH = 4096


def _plugin_settings() -> dict:
    """Read plugins.entries.telegram-send.settings from config (readonly)."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return (cfg.get("plugins") or {}).get("entries", {}).get("telegram-send", {}).get("settings", {}) or {}
    except Exception:  # noqa: BLE001 — config read must never break registration
        return {}


def _resolve_token(token_env: str) -> str:
    """Bot token via the ACTIVE profile's secret scope; fail closed, no environ fallback.

    Under gateway multiplex, ``os.environ`` holds the LAUNCH profile's values — a raw
    getenv would silently send with the wrong bot (#86905 class). ``agent.secret_scope.get_secret``
    returns the profile-scoped value; single-profile installs keep their plain environ read there.
    """
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
    return value.strip()


def _api_post(token: str, method: str, fields: dict, timeout: float = 30.0) -> dict:
    """POST one Telegram Bot API call with properly encoded form fields."""
    url = f"{API_BASE}/bot{token}/{method}"
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed api.telegram.org)
        return json.loads(resp.read().decode("utf-8"))


def _chunk(text: str, limit: int) -> list[str]:
    """Split text into <=limit chunks at paragraph/newline/space boundaries."""
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

    Returns {ok, message_id, chat_id, echoed_text} on success; raises RuntimeError
    with the Telegram error description on failure. ``echoed_text`` lets the caller
    confirm the server received exactly what was sent (the encoding-corruption check).
    """
    settings = _plugin_settings()
    token_env = settings.get("token_env") or "TELEGRAM_BOT_TOKEN"
    max_len = int(settings.get("max_message_length") or DEFAULT_MAX_LENGTH)
    chat_id = chat_id or settings.get("default_chat_id") or ""
    if not chat_id:
        raise RuntimeError("telegram_send: no chat_id given and no plugins.entries.telegram-send.settings.default_chat_id configured")
    if not text:
        raise RuntimeError("telegram_send: empty text")

    token = _resolve_token(token_env)
    results = []
    for i, chunk in enumerate(_chunk(text, max_len), start=1):
        fields = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            fields["parse_mode"] = parse_mode
        if disable_notification:
            fields["disable_notification"] = "true"
        resp = _api_post(token, "sendMessage", fields)
        if not resp.get("ok"):
            raise RuntimeError(f"telegram_send: Telegram API rejected chunk {i}: {resp.get('description', resp)}")
        result = resp.get("result") or {}
        results.append({
            "message_id": result.get("message_id"),
            "echoed_text": result.get("text", ""),
        })

    first = results[0]
    echoed = first["echoed_text"]
    # Telegram strips trailing whitespace from the delivered message, so compare
    # rstripped — anything beyond that is real corruption and must fail loudly.
    if echoed.rstrip() != _chunk(text, max_len)[0].rstrip():
        raise RuntimeError(
            "telegram_send: round-trip verification failed — Telegram echoed different text "
            "than was sent; do not trust this delivery"
        )
    return {
        "ok": True,
        "chat_id": chat_id,
        "message_ids": [r["message_id"] for r in results],
        "chunks": len(results),
        "echoed_text": echoed,
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
            "4096 chars is split into chunks. Use this instead of hand-rolled curl "
            "whenever a message must reach a Telegram chat verbatim."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text to send verbatim."},
                "chat_id": {"type": "string", "description": "Target chat id. Omit to use plugins.entries.telegram-send.settings.default_chat_id."},
                "parse_mode": {"type": "string", "description": "Optional Telegram parse_mode (MarkdownV2 or HTML). Leave empty for plain text."},
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