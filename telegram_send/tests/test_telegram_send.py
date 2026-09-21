"""E2E + unit tests for the telegram-send plugin.

Run with the Hermes venv:
    HERMES_HOME=<temp> ~/.hermes/hermes-agent/venv/bin/python -m pytest telegram_send/tests/ -v

The E2E test loads the plugin through the REAL discovery path (PluginManager
via discover_plugins) against a temp HERMES_HOME — no mocks for the wiring.
Network sends are exercised live and skipped when no test token is configured
(HERMES_TELEGRAM_SEND_TEST_TOKEN + HERMES_TELEGRAM_SEND_TEST_CHAT).
"""

from __future__ import annotations

import json
import os
import urllib.parse
from unittest import mock

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---- pure-function tests (no network, no Hermes) ---------------------------

def test_chunking_boundaries():
    import importlib.util
    spec = importlib.util.spec_from_file_location("tg_send_mod", os.path.join(PLUGIN_DIR, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    text = "a" * 10_000
    chunks = mod._chunk(text, 4096)
    assert sum(len(c) for c in chunks) == len(text)
    assert all(len(c) <= 4096 for c in chunks)

    short = "Distribution + Vertrieb & 50% <done>"
    assert mod._chunk(short, 4096) == [short]


def test_api_post_encodes_plus_correctly(monkeypatch):
    """The core contract: '+' must be percent-encoded, NEVER a bare form plus."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("tg_send_mod", os.path.join(PLUGIN_DIR, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({"ok": True, "result": {"message_id": 1, "text": captured["text_decoded"]}}).encode()

    def fake_urlopen(req, timeout=None):
        body = req.data.decode()
        captured["raw_body"] = body
        captured["text_decoded"] = urllib.parse.parse_qs(body)["text"][0]
        return FakeResp()

    monkeypatch.setattr(mod._OPENER, "open", fake_urlopen, raising=False)

    with mock.patch.object(mod, "_resolve_token", return_value="TESTTOKEN"):
        result = mod.telegram_send("Distribution + Vertrieb & 100%", chat_id="123")

    text_field = captured["raw_body"].split("text=")[1]
    assert "%2B" in text_field  # literal plus is percent-encoded on the wire
    assert text_field.startswith("Distribution+") and text_field.rstrip().endswith("100%25")
    assert captured["text_decoded"] == "Distribution + Vertrieb & 100%"
    assert result["ok"] is True
    assert result["echo_verified"] is True


# ---- plugin registration through the real discovery path -------------------

def _load_through_discovery(monkeypatch, tmp_path):
    """Load the plugin the way Hermes does: plugin dir in $HERMES_HOME/plugins, discover_plugins()."""
    hermes_home = tmp_path / "hermes-home"
    (hermes_home / "plugins").mkdir(parents=True)
    link_dir = hermes_home / "plugins" / "telegram-send"
    link_dir.symlink_to(PLUGIN_DIR, target_is_directory=True)
    (hermes_home / "config.yaml").write_text("plugins:\n  enabled:\n    - telegram-send\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import sys
    sys.path.insert(0, PLUGIN_DIR)
    sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import hermes_cli.plugins as plugins_mod
    importlib.reload(plugins_mod)
    from hermes_cli.plugins import get_plugin_manager
    manager = get_plugin_manager()
    from hermes_cli.plugins import discover_plugins as _discover
    _discover(force=True)
    return manager


def test_plugin_registers_tool(tmp_path, monkeypatch):
    manager = _load_through_discovery(monkeypatch, tmp_path)
    assert "telegram_send" in manager._plugin_tool_names


# ---- live E2E (skipped without test credentials) ---------------------------

@pytest.mark.skipif(
    not (os.getenv("HERMES_TELEGRAM_SEND_TEST_TOKEN") and os.getenv("HERMES_TELEGRAM_SEND_TEST_CHAT")),
    reason="live Telegram E2E needs HERMES_TELEGRAM_SEND_TEST_TOKEN + HERMES_TELEGRAM_SEND_TEST_CHAT",
)
def test_live_roundtrip(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("tg_send_mod", os.path.join(PLUGIN_DIR, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", os.environ["HERMES_TELEGRAM_SEND_TEST_TOKEN"])
    result = mod.telegram_send(
        "[plugin-test] roundtrip: Distribution + Vertrieb & 50% <ok>",
        chat_id=os.environ["HERMES_TELEGRAM_SEND_TEST_CHAT"],
    )
    assert result["echo_verified"] is True
    assert "Distribution + Vertrieb" in result["echoed_text"]


# ---- security-review regression tests (P0/P1 fixes) -------------------------

def _fresh_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("tg_send_mod", os.path.join(PLUGIN_DIR, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chunk_zero_or_negative_limit_raises():
    # F-002: must raise, NOT hang in an infinite loop
    mod = _fresh_mod()
    with pytest.raises(ValueError, match="must be a positive"):
        mod._chunk("abc", 0)
    with pytest.raises(ValueError, match="must be a positive"):
        mod._chunk("abc", -1)


def test_invalid_max_message_length_rejected():
    mod = _fresh_mod()
    with mock.patch.object(mod, "_plugin_settings", return_value={"max_message_length": "0"}), \
         mock.patch.object(mod, "_resolve_token", return_value="TESTTOKEN"):
        with pytest.raises(RuntimeError, match="invalid max_message_length"):
            mod.telegram_send("hi", chat_id="123")


def test_chat_id_format_validation():
    # F-003 part 1: junk chat_id is rejected before any network call
    mod = _fresh_mod()
    with mock.patch.object(mod, "_api_post_retrying") as api:
        with pytest.raises(RuntimeError, match="chat_id must be"):
            mod.telegram_send("hi", chat_id="not a chat id!!")
        api_not_called = not api.called


def test_chat_id_whitelist_blocks_unknown_recipient():
    # F-003 part 2: allowed_chat_ids whitelist — secret-exfil blocker
    mod = _fresh_mod()
    settings = {"default_chat_id": "111", "allowed_chat_ids": ["111"]}
    with mock.patch.object(mod, "_plugin_settings", return_value=settings), \
         mock.patch.object(mod, "_resolve_token", return_value="TESTTOKEN"):
        with pytest.raises(RuntimeError, match="not in plugins.entries.telegram-send"):
            mod.telegram_send("secret data", chat_id="987654321")
        # whitelisted id goes through
        with mock.patch.object(mod, "_api_post_retrying", return_value={
            "ok": True, "result": {"message_id": 1, "text": "secret data"}}):
            result = mod.telegram_send("secret data")
            assert result["ok"] is True


def test_token_not_in_error_messages(monkeypatch):
    # F-001: HTTP/network errors must never carry the token
    mod = _fresh_mod()
    token = "SUPERSECRETTOKEN123"

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            url=f"https://api.telegram.org/bot{token}/sendMessage",
            code=401, msg="Unauthorized", hdrs={}, fp=None,
        )

    monkeypatch.setattr(mod._OPENER, "open", boom, raising=False)
    import builtins

    with mock.patch.object(mod, "_resolve_token", return_value=token):
        with mock.patch.object(mod, "_plugin_settings", return_value={}):
            try:
                mod.telegram_send("hi", chat_id="123")
                raised = None
            except RuntimeError as e:
                raised = e
    assert isinstance(raised, RuntimeError)
    assert token not in str(raised)


def test_http_429_retried_then_success(monkeypatch):
    # F-005/T6: transient 429 with retry_after is retried, not raised
    mod = _fresh_mod()
    calls = {"n": 0}

    def fake_post(token, method, fields, timeout=30.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "error_code": 429, "description": "Too Many Requests",
                    "parameters": {"retry_after": 0}}
        return {"ok": True, "result": {"message_id": 7, "text": fields["text"]}}

    monkeypatch.setattr(mod, "_api_post", fake_post)
    with mock.patch.object(mod, "_resolve_token", return_value="TESTTOKEN"), \
         mock.patch.object(mod.time, "sleep"):
        result = mod.telegram_send("retry me", chat_id="123")
    assert result["ok"] is True and result["message_ids"] == [7]
    assert calls["n"] == 2


def test_http_429_persists_raises_after_max_attempts(monkeypatch):
    mod = _fresh_mod()
    monkeypatch.setattr(mod, "_api_post", lambda *a, **k: {
        "ok": False, "error_code": 429, "description": "Too Many Requests"})
    with mock.patch.object(mod, "_resolve_token", return_value="TESTTOKEN"), \
         mock.patch.object(mod.time, "sleep"):
        with pytest.raises(RuntimeError, match="after 3 attempt"):
            mod.telegram_send("hi", chat_id="123")


def test_non_transient_error_raises_immediately(monkeypatch):
    mod = _fresh_mod()
    calls = {"n": 0}

    def fake_post(token, method, fields, timeout=30.0):
        calls["n"] += 1
        return {"ok": False, "error_code": 403, "description": "Forbidden"}

    monkeypatch.setattr(mod, "_api_post", fake_post)
    with mock.patch.object(mod, "_resolve_token", return_value="TESTTOKEN"):
        with pytest.raises(RuntimeError, match="Forbidden"):
            mod.telegram_send("hi", chat_id="123")
    assert calls["n"] == 1  # no retry on non-transient errors


def test_all_chunks_echo_verified(monkeypatch):
    # F-006: corruption in chunk 2 must be caught, not just chunk 1
    mod = _fresh_mod()
    sent_texts = []

    def fake_post(token, method, fields, timeout=30.0):
        sent_texts.append(fields["text"])
        echoed = fields["text"]
        if len(sent_texts) == 2:
            echoed = echoed[:-5] + "XXXXX"  # corrupt chunk 2
        return {"ok": True, "result": {"message_id": len(sent_texts), "text": echoed}}

    monkeypatch.setattr(mod, "_api_post_retrying", fake_post)
    with mock.patch.object(mod, "_resolve_token", return_value="TESTTOKEN"), \
         mock.patch.object(mod, "_plugin_settings", return_value={}):
        with pytest.raises(RuntimeError, match="chunk 2"):
            mod.telegram_send("word " * 1500, chat_id="123")


def test_parse_mode_whitelist():
    # F-007
    mod = _fresh_mod()
    with mock.patch.object(mod, "_resolve_token", return_value="TESTTOKEN"):
        with pytest.raises(RuntimeError, match="invalid parse_mode"):
            mod.telegram_send("hi", chat_id="123", parse_mode="EvilMode")


def test_token_format_validation():
    # F-009: a non-token secret (e.g. a password) must be refused, not echoed
    mod = _fresh_mod()
    with mock.patch.object(mod, "_resolve_token", side_effect=RuntimeError(
            "telegram-send: token in the profile secret scope does not look like a Telegram bot token")):
        with pytest.raises(RuntimeError, match="does not look like a Telegram bot token"):
            mod.telegram_send("hi", chat_id="123")


def test_empty_text_and_missing_chat_id():
    mod = _fresh_mod()
    with pytest.raises(RuntimeError, match="empty text"):
        mod.telegram_send("")
    with mock.patch.object(mod, "_plugin_settings", return_value={}):
        with pytest.raises(RuntimeError, match="no chat_id"):
            mod.telegram_send("hi")