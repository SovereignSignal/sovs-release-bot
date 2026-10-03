#!/usr/bin/env python3
"""Receiver unit tests. Offline: fake sender, temp state, no live Telegram.

Do not run ``bot.py --test``. This file never does.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="release-events-unit-")
LEGACY = str(Path(TMP) / "seen.json")
V2 = str(Path(TMP) / "state.json")
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_CHAT_ID"] = "test-chat"
os.environ["RELEASE_EVENTS_TOKEN"] = "test-token"
os.environ["RELEASE_EVENTS_STATE"] = LEGACY
os.environ["RELEASE_EVENTS_STATE_V2"] = V2
os.environ["STATE_DIR"] = TMP
os.environ["STATE_FILE"] = str(Path(TMP) / "last-version.txt")
os.environ.pop("PORT", None)

import bot
import release_events as r

def _block_urlopen(*args, **kwargs):
    raise AssertionError("live network is not allowed in this test")

urllib.request.urlopen = _block_urlopen

CALLS = []

def fake_ok(text, parse_mode="HTML"):
    assert len(text) <= 4096
    CALLS.append((parse_mode, text))
    return True

bot.send_telegram = fake_ok
import runner


def _apply_env():
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
    os.environ["TELEGRAM_CHAT_ID"] = "test-chat"
    os.environ["RELEASE_EVENTS_TOKEN"] = "test-token"
    os.environ["RELEASE_EVENTS_STATE"] = LEGACY
    os.environ["RELEASE_EVENTS_STATE_V2"] = V2
    os.environ["STATE_DIR"] = TMP
    os.environ["STATE_FILE"] = str(Path(TMP) / "last-version.txt")
    os.environ.pop("RELEASE_EVENTS_LEGACY_OWNED", None)


def reset():
    _apply_env()
    for path in (Path(LEGACY), Path(V2)):
        if path.exists():
            path.unlink()
    CALLS.clear()
    bot.send_telegram = fake_ok


def event(**overrides):
    base = {
        "schema": "release-event/v1",
        "id": "software:github:example-org/example-tool:v1.2.3",
        "kind": "software",
        "name": "Example Tool",
        "version": "1.2.3",
        "source": "clawbytes",
        "url": "https://github.com/example-org/example-tool/releases/tag/v1.2.3",
    }
    base.update(overrides)
    return base


def legacy_good():
    """The assertions that already shipped with the inbox."""
    return {
        "schema": "release-event/v1",
        "id": "software:github:o/r:v1",
        "kind": "software",
        "name": "Thing",
        "version": "1.0",
        "source": "clawbytes",
        "url": "https://github.com/o/r/releases/tag/v1",
    }


def test_existing_contract():
    good = legacy_good()
    assert r.validate(good) == ""
    assert r.canonical_id(good) == good["id"]
    assert "Thing 1.0" in r.format_event(good)
    bad = dict(good)
    bad["kind"] = "paper"
    assert r.validate(bad) == "invalid kind"
    assert r.bot is bot


def test_null_and_bounds():
    good = legacy_good()
    assert r.canonical_id(dict(good, id=None)) == ""
    assert r.canonical_id({}) == ""
    assert r.validate(dict(good, name=None)) == "invalid name"
    assert r.validate(dict(good, id=None)) == "invalid id"
    assert r.validate(dict(good, kind=None)) == "invalid kind"
    assert r.validate(dict(good, version=None)) == "invalid version"
    assert r.validate(dict(good, source=None)) == "invalid source"
    assert r.validate(dict(good, url=None)) == "invalid url"
    assert r.validate(dict(good, schema=None)) == "invalid schema"
    missing = dict(good)
    del missing["name"]
    assert r.validate(missing) == "missing name"
    assert r.validate(dict(good, name="  ")) == "missing name"
    assert r.validate(dict(good, id="")) == "missing id"
    assert r.validate(dict(good, id="a" * 60000)) == "invalid id"
    assert r.validate(dict(good, id="a" * 257)) == "invalid id"
    assert r.validate(dict(good, id="a" * 256)) == ""
    assert r.validate(dict(good, id=":nope")) == "invalid id"
    assert r.validate(dict(good, name="n" * 201)) == "invalid name"
    assert r.validate(dict(good, name="n" * 200)) == ""
    assert r.validate(dict(good, version="v" * 201)) == "invalid version"
    assert r.validate(dict(good, source="s" * 201)) == "invalid source"
    assert r.validate(dict(good, schema="release-event/v0")) == "unsupported schema"
    assert r.validate(dict(good, kind=["software"])) == "invalid kind"
    assert r.validate(dict(good, summary=None)) == "invalid summary"
    assert r.validate(dict(good, summary="x" * 4001)) == "invalid summary"
    assert r.validate(dict(good, summary="x" * 4000)) == ""
    assert r.validate(dict(good, metadata=[])) == "invalid metadata"
    assert r.validate(dict(good, metadata={"blob": "y" * 9000})) == "invalid metadata"
    assert r.validate([]) == "invalid body"
    for url in (
        "http://example.com/a",
        "javascript:alert(1)",
        "ftp://example.com/a",
        'https://example.com/a"b',
        "https://example.com/a'b",
        "https://example.com/a b",
        "https://example.com/a\nb",
        "https://",
        "https:///no-host",
    ):
        assert r.validate(dict(good, url=url)) == "invalid url", url
    long_url = "https://example.com/" + ("a" * (2048 - len("https://example.com/")))
    assert len(long_url) == 2048
    assert r.validate(dict(good, url=long_url)) == ""
    assert r.validate(dict(good, url=long_url + "a")) == "invalid url"
    ampersand_url = "https://example.com/" + ("&" * (2048 - len("https://example.com/")))
    assert r.validate(dict(good, url=ampersand_url)) == ""
    rendered = r.format_event(dict(good, url=ampersand_url, summary="<" * 4000))
    assert len(rendered) <= 4096
    assert rendered.count("<") == 4
    assert rendered.endswith("</a>")


def test_escaping_and_truncation():
    good = legacy_good()
    msg = r.format_event(dict(
        good,
        name='A & B <C> "D" \'E\'',
        summary='x & y <z> "q" \'w\'',
    ))
    assert "<b>A &amp; B &lt;C&gt; &quot;D&quot; &#x27;E&#x27; 1.0</b>" in msg
    assert "x &amp; y &lt;z&gt; &quot;q&quot; &#x27;w&#x27;" in msg
    assert msg.count("<") == 4
    assert 'href="https://github.com/o/r/releases/tag/v1"' in msg
    hostile = r.format_event(dict(good, url='https://x/"><b>pwn</b>'))
    assert 'href="https://x/&quot;&gt;&lt;b&gt;pwn&lt;/b&gt;"' in hostile
    assert hostile.count("<") == 4
    huge = r.format_event(dict(good, summary="<" * 4000, name="N", version="1"))
    assert len(huge) <= 4096
    assert huge.count("<") == 4
    assert huge.endswith("</a>")
    assert "&lt;" in huge
    plain = r.html_to_plain(msg)
    assert "<b>" not in plain and "<a " not in plain
    assert "A & B <C>" in plain
    assert len(plain) <= 4096


def test_plain_fallback_sender():
    modes = []

    def fake(text, parse_mode="HTML"):
        assert len(text) <= 4096
        modes.append(parse_mode)
        if parse_mode == "HTML":
            return False
        assert "<" not in text
        assert "Example Tool" in text
        return True

    bot.send_telegram = fake
    code, msg = r.accept_event(event())
    assert (code, msg) == (202, "accepted")
    assert modes == ["HTML", None]
    stored = json.loads(Path(V2).read_text())
    assert stored["events"][event()["id"]]["status"] == "delivered"


def test_accept_once_then_duplicate():
    code, msg = r.accept_event(event())
    assert (code, msg) == (202, "accepted")
    assert len(CALLS) == 1
    code, msg = r.accept_event(event())
    assert (code, msg) == (200, "duplicate")
    assert len(CALLS) == 1
    ledger = json.loads(Path(V2).read_text())
    assert ledger["version"] == 2
    assert ledger["events"][event()["id"]]["status"] == "delivered"
    legacy = json.loads(Path(LEGACY).read_text())
    assert legacy == [event()["id"]]


def test_failed_then_retry_then_gave_up():
    html_calls = []

    def failing(text, parse_mode="HTML"):
        if parse_mode == "HTML":
            html_calls.append(event()["id"])
        return False

    bot.send_telegram = failing
    code, msg = r.accept_event(event())
    assert (code, msg) == (503, "delivery failed")
    record = json.loads(Path(V2).read_text())["events"][event()["id"]]
    assert record["status"] == "failed"
    assert record["attempts"] == 1
    assert record["delivered_at"] is None
    first_seen = record["first_seen"]

    bot.send_telegram = fake_ok
    code, msg = r.accept_event(event())
    assert (code, msg) == (202, "accepted")
    assert len(CALLS) == 1
    record = json.loads(Path(V2).read_text())["events"][event()["id"]]
    assert record["status"] == "delivered"
    assert record["attempts"] == 2
    assert record["first_seen"] == first_seen
    code, msg = r.accept_event(event())
    assert (code, msg) == (200, "duplicate")
    assert len(CALLS) == 1

    reset()
    bot.send_telegram = failing
    html_calls.clear()
    for _ in range(4):
        assert r.accept_event(event(id="software:github:example-org/example-tool:v9")) == (503, "delivery failed")
    assert r.accept_event(event(id="software:github:example-org/example-tool:v9")) == (409, "gave_up")
    assert len(html_calls) == 5
    assert r.accept_event(event(id="software:github:example-org/example-tool:v9")) == (409, "gave_up")
    assert len(html_calls) == 5
    record = json.loads(Path(V2).read_text())["events"]["software:github:example-org/example-tool:v9"]
    assert record["status"] == "failed"
    assert record["attempts"] == 5


def test_concurrent_accept():
    entered = threading.Event()
    release = threading.Event()

    def fake(text, parse_mode="HTML"):
        entered.set()
        assert release.wait(5)
        CALLS.append((parse_mode, text))
        return True

    bot.send_telegram = fake
    errors = []
    results = []

    def worker():
        try:
            results.append(r.accept_event(event()))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(5)
        deadline = time.time() + 3
        while time.time() < deadline and not any(item == (202, "in_progress") for item in results):
            time.sleep(0.01)
        assert errors == []
        assert any(item == (202, "in_progress") for item in results)
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 10
    assert results.count((202, "accepted")) == 1
    assert len(CALLS) == 1
    ledger = json.loads(Path(V2).read_text())
    assert ledger["events"][event()["id"]]["status"] == "delivered"
    legacy = json.loads(Path(LEGACY).read_text())
    assert legacy == [event()["id"]]


def test_legacy_migration_preserves_file():
    original = '["legacy-id","software:github:o/r:v1"]\n'
    Path(LEGACY).write_text(original)
    code, msg = r.accept_event(event(id="legacy-id"))
    assert (code, msg) == (200, "duplicate")
    assert CALLS == []
    assert Path(LEGACY).read_text() == original
    ledger = json.loads(Path(V2).read_text())
    assert ledger["events"]["legacy-id"]["status"] == "delivered"
    assert ledger["events"]["software:github:o/r:v1"]["status"] == "delivered"
    code, msg = r.accept_event(event(id="software:github:example-org/example-tool:v9"))
    assert (code, msg) == (202, "accepted")
    ids = json.loads(Path(LEGACY).read_text())
    assert ids[:2] == ["legacy-id", "software:github:o/r:v1"]
    assert "software:github:example-org/example-tool:v9" in ids


def test_corrupt_state_is_not_overwritten():
    Path(LEGACY).write_text("{corrupt")
    before = Path(LEGACY).read_bytes()
    buf = io.StringIO()
    with redirect_stdout(buf):
        code, msg = r.accept_event(event())
    assert (code, msg) == (503, "state unavailable")
    assert CALLS == []
    assert Path(LEGACY).read_bytes() == before
    assert not Path(V2).exists()
    assert "[EVENTS][ERROR] state corrupt" in buf.getvalue()
    assert "{corrupt" not in buf.getvalue()
    assert "test-token" not in buf.getvalue()
    assert list(Path(TMP).glob("*.corrupt*")) == []

    reset()
    Path(LEGACY).write_text("[]\n")
    Path(V2).write_text("{corrupt")
    before_v2 = Path(V2).read_bytes()
    before_legacy = Path(LEGACY).read_bytes()
    code, msg = r.accept_event(event())
    assert (code, msg) == (503, "state unavailable")
    assert CALLS == []
    assert Path(V2).read_bytes() == before_v2
    assert Path(LEGACY).read_bytes() == before_legacy


def test_readonly_directory():
    folder = Path(TMP) / "ro"
    folder.mkdir()
    os.environ["RELEASE_EVENTS_STATE"] = str(folder / "seen.json")
    os.environ["RELEASE_EVENTS_STATE_V2"] = str(folder / "state.json")
    os.chmod(folder, 0o555)
    try:
        code, msg = r.accept_event(event())
        assert (code, msg) == (503, "state unavailable")
        assert CALLS == []
        assert list(folder.iterdir()) == []
        code, body = r.readiness()
        assert code == 503
        assert body["state_writable"] is False
        assert CALLS == []
    finally:
        os.chmod(folder, 0o755)


def test_crash_window_is_not_resent():
    eid = event()["id"]
    Path(V2).write_text(json.dumps({
        "version": 2,
        "events": {
            eid: {
                "status": "pending",
                "attempts": 1,
                "first_seen": "2026-01-01T00:00:00+00:00",
                "last_attempt": "2026-01-01T00:00:00+00:00",
                "delivered_at": None,
            }
        },
    }))
    buf = io.StringIO()
    with redirect_stdout(buf):
        r.reconcile_startup()
        code, msg = r.accept_event(event())
    assert (code, msg) == (200, "duplicate")
    assert CALLS == []
    assert json.loads(Path(V2).read_text())["events"][eid]["status"] == "unknown"
    assert f"[EVENTS] id={eid} outcome=unknown" in buf.getvalue()
    assert "test-token" not in buf.getvalue()


def test_restart_subprocess():
    assert r.accept_event(event()) == (202, "accepted")
    script = """
import json, sys
import bot, release_events
calls = []
def fake(text, parse_mode="HTML"):
    calls.append(text)
    return True
bot.send_telegram = fake
event = json.loads(sys.stdin.read())
code, msg = release_events.accept_event(event)
sys.stdout.write(f"{code} {msg} {len(calls)}\\n")
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_CHAT_ID": "test-chat",
        "RELEASE_EVENTS_TOKEN": "test-token",
        "RELEASE_EVENTS_STATE": LEGACY,
        "RELEASE_EVENTS_STATE_V2": V2,
        "STATE_DIR": TMP,
        "STATE_FILE": str(Path(TMP) / "last-version.txt"),
    }
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(event()),
        text=True,
        capture_output=True,
        cwd="/workspace",
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "200 duplicate 0"
    assert "test-token" not in proc.stdout
    assert "test-token" not in proc.stderr


def test_legacy_owned():
    owned = event(metadata={"repo": "OpenClaw/OpenClaw", "package": "other"})
    code, msg = r.accept_event(owned)
    assert (code, msg) == (200, "owned_by_poller")
    assert CALLS == []
    assert not Path(V2).exists()
    code, msg = r.accept_event(event(metadata={"package": "@OpenAI/codex"}))
    assert (code, msg) == (200, "owned_by_poller")
    code, msg = r.accept_event(event(
        id="software:github:nousresearch/hermes-agent:v1",
        metadata={"repo": "NousResearch/hermes-agent"},
    ))
    assert (code, msg) == (200, "owned_by_poller")
    near = event(
        id="software:github:example-org/openclaw-extra:v1",
        metadata={"repo": "openclaw/openclaw-extra"},
    )
    assert r.accept_event(near) == (202, "accepted")
    assert len(CALLS) == 1
    os.environ["RELEASE_EVENTS_LEGACY_OWNED"] = ""
    assert r.accept_event(event(
        id="software:github:openclaw/openclaw:v1",
        metadata={"repo": "openclaw/openclaw"},
    )) == (202, "accepted")


def test_retention():
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()

    def delivered(stamp):
        return {
            "status": "delivered",
            "attempts": 1,
            "first_seen": stamp,
            "last_attempt": stamp,
            "delivered_at": stamp,
        }

    events = {f"old-{i:05d}": delivered(old) for i in range(20001)}
    events["keep-failed"] = {
        "status": "failed",
        "attempts": 2,
        "first_seen": old,
        "last_attempt": old,
        "delivered_at": None,
    }
    events["keep-unknown"] = {
        "status": "unknown",
        "attempts": 1,
        "first_seen": old,
        "last_attempt": old,
        "delivered_at": None,
    }
    kept = r._retain(events)
    assert kept["keep-failed"]["status"] == "failed"
    assert kept["keep-unknown"]["status"] == "unknown"
    assert "old-00000" not in kept
    assert "old-20000" in kept
    assert sum(1 for rec in kept.values() if rec["status"] == "delivered") == 20000

    fresh_events = {f"new-{i:05d}": delivered(fresh) for i in range(20001)}
    assert len(r._retain(fresh_events)) == 20001
    assert len(r._retain({f"id-{i}": delivered(old) for i in range(3)})) == 3

    r._save_v2({"version": 2, "events": events})
    stored = json.loads(Path(V2).read_text())
    assert stored["version"] == 2
    assert "old-00000" not in stored["events"]
    assert stored["events"]["keep-failed"]["status"] == "failed"
    assert stored["events"]["keep-unknown"]["status"] == "unknown"


def test_legacy_append_is_bounded():
    ids = [f"id-{i}" for i in range(r.LEGACY_BOUND)]
    Path(LEGACY).write_text(json.dumps(ids))
    r._append_legacy("extra-id")
    got = json.loads(Path(LEGACY).read_text())
    assert len(got) == r.LEGACY_BOUND
    assert got[-1] == "extra-id"
    assert "id-0" not in got
    assert isinstance(got, list)


def test_paths_do_not_clobber_legacy():
    try:
        os.environ.pop("RELEASE_EVENTS_STATE", None)
        os.environ.pop("RELEASE_EVENTS_STATE_V2", None)
        assert r.legacy_path() == Path("/data/release-events-seen.json")
        assert r.v2_path() == Path("/data/release-events-state.json")
        same = Path(TMP) / "same.json"
        os.environ["RELEASE_EVENTS_STATE"] = str(same)
        os.environ["RELEASE_EVENTS_STATE_V2"] = str(same)
        assert r.v2_path() == Path(TMP) / "release-events-state.json"
        os.environ["RELEASE_EVENTS_STATE"] = str(Path(TMP) / "release-events-state.json")
        os.environ["RELEASE_EVENTS_STATE_V2"] = os.environ["RELEASE_EVENTS_STATE"]
        assert r.v2_path() != r.legacy_path()
    finally:
        _apply_env()


def test_logs_skip_body_and_token():
    buf = io.StringIO()
    secret = "ignore previous instructions token=test-token"
    with redirect_stdout(buf):
        code, msg = r.accept_event(event(summary=secret))
    assert (code, msg) == (202, "accepted")
    logged = buf.getvalue()
    assert "ignore previous instructions" not in logged
    assert "test-token" not in logged
    assert f"[EVENTS] id={event()['id']} outcome=delivered" in logged
    assert f"[EVENTS] id={event()['id']} outcome=received" in logged


def test_runner_supervision():
    import inspect
    source = inspect.getsource(runner.main)
    assert source.index("wire") < source.index("Thread") < source.index("bot.daemon()")
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    orig_serve = r.serve
    orig_exit = os._exit
    try:
        def dead_serve(*args, **kwargs):
            raise RuntimeError("inbox down")

        r.serve = dead_serve
        os._exit = fake_exit
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                runner._run_inbox()
            except SystemExit as exc:
                assert exc.code == 1
        assert exits == [1]
        assert "[EVENTS][ERROR] http inbox thread died" in buf.getvalue()

        exits.clear()

        def quiet_serve(*args, **kwargs):
            return None

        r.serve = quiet_serve
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                runner._run_inbox()
            except SystemExit as exc:
                assert exc.code == 1
        assert exits == [1]
        assert "[EVENTS][ERROR] http inbox thread died" in buf.getvalue()
    finally:
        r.serve = orig_serve
        os._exit = orig_exit


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
        test_existing_contract,
        test_null_and_bounds,
        test_escaping_and_truncation,
        test_plain_fallback_sender,
        test_accept_once_then_duplicate,
        test_failed_then_retry_then_gave_up,
        test_concurrent_accept,
        test_legacy_migration_preserves_file,
        test_corrupt_state_is_not_overwritten,
        test_readonly_directory,
        test_crash_window_is_not_resent,
        test_restart_subprocess,
        test_legacy_owned,
        test_retention,
        test_legacy_append_is_bounded,
        test_paths_do_not_clobber_legacy,
        test_logs_skip_body_and_token,
        test_runner_supervision,
    ]
    results = [run(fn.__name__, fn) for fn in tests]
    if all(results):
        print(f"\nAll tests passed. ({len(results)})")
        return 0
    print(f"\n{results.count(False)} failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
