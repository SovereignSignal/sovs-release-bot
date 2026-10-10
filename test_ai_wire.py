#!/usr/bin/env python3
"""AI Wire ingest: mapping, flag-off no-op, and push failures never raise.

Offline. No live Telegram and no live registry. Run: python3 test_ai_wire.py
"""
import io
import json
import os
import tempfile
import traceback
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="ai-wire-")
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_CHAT_ID"] = "test-chat"
os.environ["RELEASE_EVENTS_TOKEN"] = "test-token"
os.environ["RELEASE_EVENTS_STATE"] = str(Path(TMP) / "seen.json")
os.environ["RELEASE_EVENTS_STATE_V2"] = str(Path(TMP) / "state.json")
os.environ["STATE_DIR"] = TMP
os.environ["STATE_FILE"] = str(Path(TMP) / "last-version.txt")
os.environ.pop("AI_WIRE_ENABLED", None)
os.environ.pop("AI_WIRE_URL", None)
os.environ.pop("AI_WIRE_INGEST_TOKEN", None)
os.environ.pop("OLLAMA_API_KEY", None)
os.environ.pop("WATCH_PRERELEASES", None)

import bot
import release_events as inbox

bot.OLLAMA_API_KEY = ""
_REAL_SEND = bot.send_telegram
_REAL_NPM = bot.get_npm_latest
_REAL_NEWEST = bot.newest_gh_release
_REAL_GH = bot.get_gh_release

WIRE = "https://wire.example"
TOKEN = "super-secret-token"
INGEST = WIRE + "/api/ingest/items"


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _clear_wire_env():
    os.environ.pop("AI_WIRE_ENABLED", None)
    os.environ.pop("AI_WIRE_URL", None)
    os.environ.pop("AI_WIRE_INGEST_TOKEN", None)


def _enable_wire():
    os.environ["AI_WIRE_ENABLED"] = "true"
    os.environ["AI_WIRE_URL"] = WIRE + "/"
    os.environ["AI_WIRE_INGEST_TOKEN"] = TOKEN


def _install_urlopen(handler):
    urllib.request.urlopen = handler


def _forbid_network(*args, **kwargs):
    raise AssertionError("network is not allowed in this test")


def reset():
    _clear_wire_env()
    os.environ.pop("WATCH_PRERELEASES", None)
    os.environ.pop("RELEASE_EVENTS_LEGACY_OWNED", None)
    bot.OLLAMA_API_KEY = ""
    bot.send_telegram = _REAL_SEND
    bot.get_npm_latest = _REAL_NPM
    bot.newest_gh_release = _REAL_NEWEST
    bot.get_gh_release = _REAL_GH
    for name in ("seen.json", "state.json", "last-version.txt", "last-version-codex.txt"):
        path = Path(TMP) / name
        if path.exists():
            path.unlink()
    _install_urlopen(_forbid_network)


def _event(**overrides):
    base = {
        "schema": "release-event/v1",
        "id": "software:github:openai/codex:v0.50.0",
        "kind": "software",
        "name": "Codex",
        "version": "0.50.0",
        "source": "clawbytes",
        "url": "https://github.com/openai/codex/releases/tag/v0.50.0",
        "summary": "**Ship** the rust runtime.",
        "metadata": {
            "package": "@openai/codex",
            "published_at": "2026-10-09T00:00:00Z",
        },
    }
    base.update(overrides)
    return base


def test_github_poll_mapping():
    import ai_wire

    item = ai_wire.item_for_poll(
        package_name="@openai/codex",
        version="0.50.0",
        title="Codex 0.50.0",
        summary="• Ship the rust runtime.",
        url="https://github.com/openai/codex/releases/tag/v0.50.0",
        published_at="2026-10-09T00:00:00Z",
        github_repo="OpenAI/Codex",
        github_tag="v0.50.0",
    )
    assert item["source_bot"] == "release-bot"
    assert item["channel"] == "release-alerts"
    assert item["kind"] == "tool_release"
    assert item["canonical_key"] == "release:openai/codex@v0.50.0"
    assert item["title"] == "Codex 0.50.0"
    assert item["url"] == "https://github.com/openai/codex/releases/tag/v0.50.0"
    assert item["summary"] == "• Ship the rust runtime."
    assert item["published_at"] == "2026-10-09T00:00:00Z"
    assert item["org"] == "OpenAI"
    assert item["extra"] == {"package": "@openai/codex", "version": "0.50.0"}
    assert "channel_post_url" not in item


def test_npm_only_poll_mapping():
    import ai_wire

    item = ai_wire.item_for_poll(
        package_name="@OpenAI/Codex",
        version="0.150.1",
        title="Codex 0.150.1",
        summary="• npm only",
        url="https://www.npmjs.com/package/@OpenAI/Codex/v/0.150.1",
        published_at="unknown",
        github_repo="",
        github_tag="",
    )
    assert item["canonical_key"] == "release:npm/@openai/codex@0.150.1"
    assert item["url"] == "https://www.npmjs.com/package/@OpenAI/Codex/v/0.150.1"
    assert item["org"] == "OpenAI"
    assert item["extra"] == {"package": "@OpenAI/Codex", "version": "0.150.1"}
    assert "published_at" not in item
    assert "channel_post_url" not in item


def test_hermes_tag_and_summary_clip():
    import ai_wire

    item = ai_wire.item_for_poll(
        package_name="hermes-agent",
        version="0.20.6",
        title="Hermes Agent 0.20.6",
        summary="x" * 700,
        url="https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.27",
        published_at="2026-08-27T12:06:53Z",
        github_repo="NousResearch/hermes-agent",
        github_tag="V2026.8.27",
    )
    assert item["canonical_key"] == "release:nousresearch/hermes-agent@v2026.8.27"
    assert item["org"] == "NousResearch"
    assert item["extra"] == {"package": "hermes-agent", "version": "0.20.6"}
    assert len(item["summary"]) == 600
    assert "channel_post_url" not in item


def test_event_github_release_page():
    import ai_wire

    item = ai_wire.item_for_event(
        _event(url="https://github.com/openai/codex/releases/tag/v0.50.0?utm=1"),
        "• Ship the rust runtime.",
    )
    assert item["source_bot"] == "release-bot"
    assert item["channel"] == "release-alerts"
    assert item["kind"] == "tool_release"
    assert item["canonical_key"] == "release:openai/codex@v0.50.0"
    assert item["title"] == "Codex 0.50.0"
    assert item["url"] == "https://github.com/openai/codex/releases/tag/v0.50.0?utm=1"
    assert item["summary"] == "• Ship the rust runtime."
    assert item["published_at"] == "2026-10-09T00:00:00Z"
    assert item["org"] == "openai"
    assert item["extra"] == {"package": "@openai/codex", "version": "0.50.0"}
    assert "channel_post_url" not in item
    assert item["source_bot"] != "clawbytes"


def test_event_npm_only():
    import ai_wire

    item = ai_wire.item_for_event(
        _event(
            id="software:npm:left-pad:1.0.0",
            name="left-pad",
            version="1.0.0",
            url="https://example.com/left-pad",
            metadata={"package": "left-pad"},
            summary="pad",
        ),
        "• pad strings",
    )
    assert item["canonical_key"] == "release:npm/left-pad@1.0.0"
    assert item["url"] == "https://example.com/left-pad"
    assert item["extra"] == {"package": "left-pad", "version": "1.0.0"}
    assert "org" not in item
    assert "channel_post_url" not in item


def test_flag_off_is_noop():
    import ai_wire

    calls = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise AssertionError("flag off must not call the registry")

    _install_urlopen(urlopen)
    item = {"canonical_key": "release:openai/codex@v0.50.0", "title": "Codex", "url": "https://example.com"}
    buf = io.StringIO()
    for value in (None, "", "false", "0", "no", "off", "FALSE"):
        if value is None:
            os.environ.pop("AI_WIRE_ENABLED", None)
        else:
            os.environ["AI_WIRE_ENABLED"] = value
        os.environ["AI_WIRE_URL"] = WIRE
        os.environ["AI_WIRE_INGEST_TOKEN"] = TOKEN
        with redirect_stdout(buf):
            ai_wire.push_item(item)
    assert calls == []
    assert buf.getvalue() == ""
    assert "ai_wire push" not in buf.getvalue()


def test_push_success_logs_upserted_and_retries_once():
    import ai_wire

    _enable_wire()
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req)
        assert timeout == 5
        if len(calls) == 1:
            raise TimeoutError("timed out")
        body = json.dumps({"upserted": 1, "created": 1, "keys": ["release:openai/codex@v0.50.0"]}).encode()
        return _Resp(body)

    _install_urlopen(urlopen)
    item = ai_wire.item_for_poll(
        package_name="@openai/codex",
        version="0.50.0",
        title="Codex 0.50.0",
        summary="• Ship the rust runtime.",
        url="https://github.com/openai/codex/releases/tag/v0.50.0",
        published_at="2026-10-09T00:00:00Z",
        github_repo="openai/codex",
        github_tag="v0.50.0",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        ai_wire.push_item(item)
    assert len(calls) == 2
    req = calls[-1]
    assert req.full_url == INGEST
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert json.loads(req.data.decode()) == item
    assert TOKEN not in buf.getvalue()
    assert "ai_wire push ok n=1" in buf.getvalue()
    assert "ai_wire push failed:" not in buf.getvalue()


def test_push_failure_never_raises():
    import ai_wire

    _enable_wire()
    calls = []

    def urlopen(req, timeout=None):
        calls.append(timeout)
        raise TimeoutError("timed out")

    _install_urlopen(urlopen)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ai_wire.push_item({"title": "x", "url": "https://example.com", "canonical_key": "release:npm/left-pad@1.0.0"})
    assert calls == [5, 5]
    text = buf.getvalue()
    assert "ai_wire push failed: timeout" in text
    assert TOKEN not in text

    calls.clear()

    def http_fail(req, timeout=None):
        calls.append(timeout)
        raise urllib.error.HTTPError(INGEST, 401, "no", {}, None)

    _install_urlopen(http_fail)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ai_wire.push_item({"title": "x", "url": "https://example.com", "canonical_key": "k"})
    assert len(calls) == 2
    assert "ai_wire push failed: HTTP 401" in buf.getvalue()
    assert TOKEN not in buf.getvalue()

    os.environ["AI_WIRE_INGEST_TOKEN"] = ""
    _install_urlopen(_forbid_network)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ai_wire.push_item({"title": "x"})
    assert "ai_wire push failed:" in buf.getvalue()
    assert "ai_wire push ok" not in buf.getvalue()


def _codex_npm(version="0.2.0"):
    return {
        "version": version,
        "published": "2026-10-01T12:00:00Z",
        "description": "",
        "repository": "",
        "package": "@openai/codex",
    }


def _codex_release(tag, body, name=None):
    return {
        "tag_name": tag,
        "name": name if name is not None else tag,
        "html_url": f"https://github.com/openai/codex/releases/tag/{tag}",
        "published_at": "2026-10-01T12:00:00Z",
        "body": body,
        "prerelease": False,
        "draft": False,
    }


def _capture_ingest():
    posts = []

    def urlopen(req, timeout=None):
        posts.append((req, timeout))
        payload = json.dumps({"upserted": 1, "created": 1, "keys": ["k"]}).encode()
        return _Resp(payload)

    _install_urlopen(urlopen)
    return posts


def test_poller_pushes_github_release_only_after_send():
    _enable_wire()
    posts = _capture_ingest()
    sent = []

    def send(text, parse_mode="HTML"):
        sent.append(text)
        return True

    bot.send_telegram = send
    bot.get_npm_latest = lambda package_key: _codex_npm("0.2.0")
    release = _codex_release("rust-v0.2.0", "Adds a faster sandbox.")
    bot.newest_gh_release = lambda package_key: release
    bot.get_gh_release = lambda package_key, version: release
    bot.save_state("codex", {"npm": "0.1.0", "github_tag": "rust-v0.1.0"})
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert bot.check_package("codex") is True
    assert len(sent) == 1
    assert len(posts) == 1
    req, timeout = posts[0]
    assert timeout == 5
    item = json.loads(req.data.decode())
    assert item["canonical_key"] == "release:openai/codex@rust-v0.2.0"
    assert item["source_bot"] == "release-bot"
    assert item["channel"] == "release-alerts"
    assert item["kind"] == "tool_release"
    assert item["title"] == "Codex 0.2.0"
    assert item["url"] == "https://github.com/openai/codex/releases/tag/rust-v0.2.0"
    assert item["summary"] == "• Adds a faster sandbox."
    assert item["published_at"] == "2026-10-01T12:00:00Z"
    assert item["org"] == "openai"
    assert item["extra"] == {"package": "@openai/codex", "version": "0.2.0"}
    assert "channel_post_url" not in item
    assert "ai_wire push ok n=1" in buf.getvalue()
    stored = bot.load_state("codex")
    assert stored["npm"] == "0.2.0"


def test_poller_npm_only_and_skips_matching_github_repost():
    _enable_wire()
    posts = _capture_ingest()
    bot.send_telegram = lambda text, parse_mode="HTML": True
    bot.get_npm_latest = lambda package_key: _codex_npm("0.2.0")
    bot.newest_gh_release = lambda package_key: None
    bot.get_gh_release = lambda package_key, version: None
    bot.save_state("codex", {"npm": "0.1.0", "github_tag": ""})
    assert bot.check_package("codex") is True
    assert len(posts) == 1
    item = json.loads(posts[0][0].data.decode())
    assert item["canonical_key"] == "release:npm/@openai/codex@0.2.0"
    assert item["url"] == "https://www.npmjs.com/package/@openai/codex/v/0.2.0"
    assert "channel_post_url" not in item

    posts.clear()
    bot.get_npm_latest = lambda package_key: _codex_npm("0.2.0")
    bot.newest_gh_release = lambda package_key: _codex_release("rust-v0.2.0", "Adds a faster sandbox.")
    bot.get_gh_release = lambda package_key, version: None
    assert bot.check_package("codex") is False
    assert posts == []


def test_poller_send_failure_does_not_push_or_advance():
    _enable_wire()
    posts = _capture_ingest()
    bot.send_telegram = lambda text, parse_mode="HTML": False
    bot.get_npm_latest = lambda package_key: _codex_npm("0.3.0")
    bot.newest_gh_release = lambda package_key: None
    bot.get_gh_release = lambda package_key, version: None
    bot.save_state("codex", {"npm": "0.1.0", "github_tag": ""})
    assert bot.check_package("codex") is False
    assert posts == []
    assert bot.load_state("codex")["npm"] == "0.1.0"


def test_poller_registry_failure_still_records_send():
    _enable_wire()

    def urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    _install_urlopen(urlopen)
    bot.send_telegram = lambda text, parse_mode="HTML": True
    bot.get_npm_latest = lambda package_key: _codex_npm("0.4.0")
    bot.newest_gh_release = lambda package_key: None
    bot.get_gh_release = lambda package_key, version: None
    bot.save_state("codex", {"npm": "0.1.0", "github_tag": ""})
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert bot.check_package("codex") is True
    assert bot.load_state("codex")["npm"] == "0.4.0"
    assert "ai_wire push failed: timeout" in buf.getvalue()
    assert TOKEN not in buf.getvalue()


def test_direct_telegram_send_does_not_push():
    """Preview and other non-alert sends stay off the registry."""
    _enable_wire()
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(b'{"ok": true}')

    _install_urlopen(urlopen)
    assert bot.send_telegram("hello") is True
    assert calls
    assert all("/api/ingest/items" not in url for url in calls)


def _open_forwarded_events():
    """Codex is on the poller's deny list. These tests use a forwarded copy."""
    os.environ["RELEASE_EVENTS_LEGACY_OWNED"] = ""


def test_inbox_pushes_after_accept_and_not_on_duplicate_or_failure():
    _open_forwarded_events()
    _enable_wire()
    posts = _capture_ingest()
    calls = []

    def send(text, parse_mode="HTML"):
        calls.append(parse_mode)
        return True

    bot.send_telegram = send
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert inbox.accept_event(_event()) == (202, "accepted")
    assert len(posts) == 1
    item = json.loads(posts[0][0].data.decode())
    assert item["canonical_key"] == "release:openai/codex@v0.50.0"
    assert item["kind"] == "tool_release"
    assert item["source_bot"] == "release-bot"
    assert item["channel"] == "release-alerts"
    assert item["title"] == "Codex 0.50.0"
    assert item["url"] == "https://github.com/openai/codex/releases/tag/v0.50.0"
    assert item["summary"] == "• Ship the rust runtime."
    assert item["published_at"] == "2026-10-09T00:00:00Z"
    assert item["org"] == "openai"
    assert item["extra"] == {"package": "@openai/codex", "version": "0.50.0"}
    assert "channel_post_url" not in item
    assert "ai_wire push ok n=1" in buf.getvalue()

    with redirect_stdout(io.StringIO()):
        assert inbox.accept_event(_event()) == (200, "duplicate")
    assert len(posts) == 1

    def fail(text, parse_mode="HTML"):
        return False

    bot.send_telegram = fail
    before = len(posts)
    with redirect_stdout(io.StringIO()):
        code, msg = inbox.accept_event(_event(id="software:github:openai/codex:v9"))
    assert (code, msg) == (503, "delivery failed")
    assert len(posts) == before


def test_inbox_registry_failure_still_accepts():
    _open_forwarded_events()
    _enable_wire()

    def urlopen(req, timeout=None):
        raise urllib.error.URLError("boom")

    _install_urlopen(urlopen)
    bot.send_telegram = lambda text, parse_mode="HTML": True
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert inbox.accept_event(_event(id="software:github:openai/codex:v8")) == (202, "accepted")
    stored = json.loads(Path(os.environ["RELEASE_EVENTS_STATE_V2"]).read_text())
    assert stored["events"]["software:github:openai/codex:v8"]["status"] == "delivered"
    assert "ai_wire push failed:" in buf.getvalue()
    assert TOKEN not in buf.getvalue()


def test_inbox_flag_off_still_delivers():
    _open_forwarded_events()
    posts = []

    def urlopen(req, timeout=None):
        posts.append(req.full_url)
        raise AssertionError("flag off must not call the registry")

    _install_urlopen(urlopen)
    bot.send_telegram = lambda text, parse_mode="HTML": True
    assert inbox.accept_event(_event(id="software:github:openai/codex:v7")) == (202, "accepted")
    assert posts == []


def test_owned_by_poller_does_not_push():
    _enable_wire()
    posts = _capture_ingest()
    bot.send_telegram = lambda text, parse_mode="HTML": True
    owned = _event(
        id="software:github:openclaw/openclaw:v1",
        metadata={"repo": "openclaw/openclaw", "package": "openclaw"},
    )
    assert inbox.accept_event(owned) == (200, "owned_by_poller")
    assert posts == []


def run(name, fn):
    reset()
    try:
        fn()
    except Exception as exc:
        print(f"FAIL: {name}: {exc}")
        traceback.print_exc()
        return False
    print(f"ok: {name}")
    return True


def main():
    tests = [
        test_github_poll_mapping,
        test_npm_only_poll_mapping,
        test_hermes_tag_and_summary_clip,
        test_event_github_release_page,
        test_event_npm_only,
        test_flag_off_is_noop,
        test_push_success_logs_upserted_and_retries_once,
        test_push_failure_never_raises,
        test_poller_pushes_github_release_only_after_send,
        test_poller_npm_only_and_skips_matching_github_repost,
        test_poller_send_failure_does_not_push_or_advance,
        test_poller_registry_failure_still_records_send,
        test_direct_telegram_send_does_not_push,
        test_inbox_pushes_after_accept_and_not_on_duplicate_or_failure,
        test_inbox_registry_failure_still_accepts,
        test_inbox_flag_off_still_delivers,
        test_owned_by_poller_does_not_push,
    ]
    results = [run(fn.__name__, fn) for fn in tests]
    if all(results):
        print(f"\nAll tests passed. ({len(results)})")
        return 0
    print(f"\n{results.count(False)} failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
