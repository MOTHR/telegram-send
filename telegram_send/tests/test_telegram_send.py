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

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

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