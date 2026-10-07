#!/usr/bin/env python3
"""Import-mode HTTP tests for the release inbox.

Mirrors runner.py: import bot, import release_events, wire(bot), then serve
on 127.0.0.1. Fake sender, temp state, no live Telegram, no production host.
Do not run ``bot.py --test``. This file never does.
"""
import json
import os
import socket
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="release-events-http-")
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
import release_events

bot.OLLAMA_API_KEY = ""

release_events.wire(bot)

_ORIGINAL_URL_OPEN = urllib.request.urlopen
_ORIGINAL_CONNECT = socket.create_connection


def _guard_urlopen(url, *args, **kwargs):
    target = url.full_url if isinstance(url, urllib.request.Request) else str(url)
    host = urllib.parse.urlparse(target).hostname
    if host != "127.0.0.1" or "railway.app" in target or "api.telegram.org" in target:
        raise AssertionError("live network is not allowed")
    return _ORIGINAL_URL_OPEN(url, *args, **kwargs)


def _guard_connect(address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) else address
    if host != "127.0.0.1":
        raise AssertionError("live network is not allowed")
    return _ORIGINAL_CONNECT(address, *args, **kwargs)


urllib.request.urlopen = _guard_urlopen
socket.create_connection = _guard_connect


class Sender:
    def __init__(self):
        self.calls = []
        self.delivered = 0
        self.mode = "ok"
        self.delay = 0

    def __call__(self, text, parse_mode="HTML"):
        assert len(text) <= 4096
        self.calls.append(parse_mode)
        if self.delay:
            time.sleep(self.delay)
        if self.mode == "fail":
            return False
        if self.mode == "html_then_plain":
            ok = parse_mode != "HTML"
        else:
            ok = True
        if ok:
            self.delivered += 1
        return ok

    def reset(self):
        self.calls.clear()
        self.delivered = 0
        self.mode = "ok"
        self.delay = 0


sender = Sender()
bot.send_telegram = sender

PORT = None
READY = threading.Event()
BOX = {}
THREAD = None


def _serve():
    try:
        release_events.serve("127.0.0.1", 0, _on_bind)
    except Exception as exc:
        BOX["error"] = exc
        READY.set()


def _on_bind(httpd):
    global PORT
    PORT = httpd.server_address[1]
    BOX["httpd"] = httpd
    READY.set()


FIXTURE = {
    "schema": "release-event/v1",
    "id": "software:github:example-org/example-tool:v1.2.3",
    "kind": "software",
    "name": "Example Tool",
    "version": "1.2.3",
    "source": "clawbytes",
    "source_type": "github_release",
    "url": "https://github.com/example-org/example-tool/releases/tag/v1.2.3",
}


def reset():
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
    os.environ["TELEGRAM_CHAT_ID"] = "test-chat"
    os.environ["RELEASE_EVENTS_TOKEN"] = "test-token"
    os.environ["RELEASE_EVENTS_STATE"] = LEGACY
    os.environ["RELEASE_EVENTS_STATE_V2"] = V2
    os.environ.pop("RELEASE_EVENTS_LEGACY_OWNED", None)
    sender.reset()
    for path in (Path(LEGACY), Path(V2)):
        if path.exists():
            path.unlink()


def _url(path):
    target = f"http://127.0.0.1:{PORT}{path}"
    assert urllib.parse.urlparse(target).hostname == "127.0.0.1"
    return target


def get(path):
    with urllib.request.urlopen(_url(path), timeout=5) as resp:
        return resp.status, resp.read().decode()


def get_allowing_error(path):
    try:
        return get(path)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def post(body=None, token="test-token", content_type="application/json", raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    headers = {"Content-Type": content_type}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(_url("/v1/releases"), data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _content_length(head_bytes):
    for line in head_bytes.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            return int(line.split(b":", 1)[1].strip())
    return None


def raw_http(head, body=b""):
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    sock.settimeout(5)
    try:
        sock.sendall(head.encode() + body)
        data = b""
        while b"\r\n\r\n" not in data:
            part = sock.recv(4096)
            if not part:
                break
            data += part
        if b"\r\n\r\n" not in data:
            return data
        head_bytes, rest = data.split(b"\r\n\r\n", 1)
        need = _content_length(head_bytes)
        if need is None:
            return data
        while len(rest) < need:
            part = sock.recv(4096)
            if not part:
                break
            rest += part
        return head_bytes + b"\r\n\r\n" + rest
    finally:
        sock.close()


def status_of(response):
    return int(response.split(b"\r\n", 1)[0].split()[1])


def test_import_mode_post_delivers_once():
    # This is the request that used to raise NameError and drop the socket.
    assert get("/health") == (200, "ok")
    assert sender.delivered == 0
    code, body = post(FIXTURE)
    assert (code, body) == (202, "accepted")
    assert sender.delivered == 1
    assert Path(V2).exists()
    stored = json.loads(Path(V2).read_text())
    assert stored["events"][FIXTURE["id"]]["status"] == "delivered"


def test_auth():
    assert post(FIXTURE, token=None)[0] == 401
    assert post(FIXTURE, token="wrong-token") == (401, "")
    os.environ["RELEASE_EVENTS_TOKEN"] = ""
    try:
        assert post(FIXTURE, token="test-token")[0] == 401
    finally:
        os.environ["RELEASE_EVENTS_TOKEN"] = "test-token"
    assert sender.delivered == 0


def test_rejected_events_do_not_send():
    cases = []
    for field in ("schema", "id", "kind", "name", "version", "source", "url"):
        cases.append((dict(FIXTURE, **{field: None}), f"invalid {field}"))
    missing = dict(FIXTURE)
    del missing["name"]
    cases.append((missing, "missing name"))
    cases.append((dict(FIXTURE, schema="nope"), "unsupported schema"))
    cases.append((dict(FIXTURE, kind="paper"), "invalid kind"))
    cases.append((dict(FIXTURE, url="javascript:alert(1)"), "invalid url"))
    cases.append((dict(FIXTURE, url="ftp://example.com/a"), "invalid url"))
    cases.append((dict(FIXTURE, url="http://example.com/a"), "invalid url"))
    cases.append((dict(FIXTURE, url='https://example.com/a"b'), "invalid url"))
    cases.append((dict(FIXTURE, url="https://example.com/" + ("a" * 3000)), "invalid url"))
    cases.append((dict(FIXTURE, id="a" * 60000), "invalid id"))
    cases.append((dict(FIXTURE, summary="s" * 4001), "invalid summary"))
    for body, reason in cases:
        code, text = post(body)
        assert (code, text) == (400, reason), (body.get("id") if isinstance(body, dict) else body, code, text, reason)
        assert sender.delivered == 0
        assert sender.calls == []
    for raw, reason in ((b"{", "invalid json"), (b"", None), (b"not-json", "invalid json")):
        if raw == b"":
            response = raw_http(
                "POST /v1/releases HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Authorization: Bearer test-token\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            )
            assert status_of(response) == 400
        else:
            assert post(raw=raw) == (400, reason)
    for payload in ([], None, "x", 1, True):
        assert post(payload) == (400, "invalid body")
    assert sender.delivered == 0


def test_body_limits():
    missing = raw_http(
        "POST /v1/releases HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Authorization: Bearer test-token\r\n"
        "Content-Type: application/json\r\n"
        "Connection: close\r\n\r\n"
    )
    assert status_of(missing) == 411
    chunked = raw_http(
        "POST /v1/releases HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Authorization: Bearer test-token\r\n"
        "Content-Type: application/json\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n\r\n"
        "0\r\n\r\n"
    )
    assert status_of(chunked) == 411
    huge = raw_http(
        "POST /v1/releases HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Authorization: Bearer test-token\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 70000\r\n"
        "Connection: close\r\n\r\n"
    )
    assert status_of(huge) == 400
    assert b"bad length" in huge
    assert post(FIXTURE, content_type="text/plain") == (415, "unsupported media type")
    assert sender.delivered == 0


def test_duplicate_post():
    assert post(FIXTURE) == (202, "accepted")
    assert post(FIXTURE) == (200, "duplicate")
    assert sender.delivered == 1
    legacy = json.loads(Path(LEGACY).read_text())
    assert legacy == [FIXTURE["id"]]


def test_concurrent_posts():
    sender.delay = 0.2
    errors = []
    results = []

    def worker():
        try:
            results.append(post(FIXTURE))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    assert sender.delivered == 1
    assert results.count((202, "accepted")) == 1
    assert any(item == (202, "in_progress") for item in results)
    assert all(item[0] in (200, 202) for item in results)
    ledger = json.loads(Path(V2).read_text())
    assert ledger["events"][FIXTURE["id"]]["status"] == "delivered"
    legacy = json.loads(Path(LEGACY).read_text())
    assert isinstance(legacy, list)
    assert FIXTURE["id"] in legacy


def test_failed_delivery_then_retry():
    sender.mode = "fail"
    assert post(FIXTURE) == (503, "delivery failed")
    record = json.loads(Path(V2).read_text())["events"][FIXTURE["id"]]
    assert record["status"] == "failed"
    assert sender.delivered == 0
    sender.mode = "ok"
    assert post(FIXTURE) == (202, "accepted")
    assert sender.delivered == 1
    assert post(FIXTURE) == (200, "duplicate")
    assert sender.delivered == 1
    assert json.loads(Path(V2).read_text())["events"][FIXTURE["id"]]["status"] == "delivered"


def test_legacy_list_is_duplicate():
    Path(LEGACY).write_text(json.dumps([FIXTURE["id"]]) + "\n")
    before = Path(LEGACY).read_bytes()
    assert post(FIXTURE) == (200, "duplicate")
    assert sender.delivered == 0
    assert Path(LEGACY).read_bytes() == before
    assert json.loads(Path(V2).read_text())["events"][FIXTURE["id"]]["status"] == "delivered"


def test_corrupt_and_readonly():
    Path(V2).write_text("{corrupt")
    before = Path(V2).read_bytes()
    assert get("/health") == (200, "ok")
    ready_code, ready_body = get_allowing_error("/ready")
    assert ready_code == 503
    ready = json.loads(ready_body)
    assert ready["state_writable"] is False
    assert set(ready) == {"token", "telegram", "state_writable", "worker"}
    assert post(FIXTURE) == (503, "state unavailable")
    assert Path(V2).read_bytes() == before
    assert sender.delivered == 0
    assert list(Path(TMP).glob("*.corrupt*")) == []

    reset()
    folder = Path(TMP) / "ro"
    folder.mkdir()
    os.environ["RELEASE_EVENTS_STATE"] = str(folder / "seen.json")
    os.environ["RELEASE_EVENTS_STATE_V2"] = str(folder / "state.json")
    os.chmod(folder, 0o555)
    try:
        assert post(FIXTURE) == (503, "state unavailable")
        assert sender.delivered == 0
        assert list(folder.iterdir()) == []
    finally:
        os.chmod(folder, 0o755)


def test_ready_booleans_do_not_call_sender():
    code, body = get("/ready")
    parsed = json.loads(body)
    assert code == 200
    assert parsed == {
        "token": True,
        "telegram": True,
        "state_writable": True,
        "worker": True,
    }
    assert "test-token" not in body
    assert sender.calls == []
    os.environ["RELEASE_EVENTS_TOKEN"] = ""
    try:
        code, body = get_allowing_error("/ready")
        parsed = json.loads(body)
        assert code == 503
        assert parsed["token"] is False
        assert parsed["telegram"] is True
        assert parsed["worker"] is True
        assert isinstance(parsed["state_writable"], bool)
    finally:
        os.environ["RELEASE_EVENTS_TOKEN"] = "test-token"
    os.environ["TELEGRAM_CHAT_ID"] = ""
    try:
        code, body = get_allowing_error("/ready")
        parsed = json.loads(body)
        assert code == 503
        assert parsed["telegram"] is False
        assert "test-token" not in body
    finally:
        os.environ["TELEGRAM_CHAT_ID"] = "test-chat"
    assert sender.delivered == 0
    assert sender.calls == []


def test_legacy_owned_http():
    owned = dict(FIXTURE, metadata={"repo": "anthropics/claude-code"})
    assert post(owned) == (200, "owned_by_poller")
    assert sender.delivered == 0
    assert not Path(V2).exists()
    other = dict(FIXTURE, metadata={"package": "some-other-package"})
    assert post(other) == (202, "accepted")
    assert sender.delivered == 1


def test_handler_error_returns_500():
    orig = release_events.accept_event

    def boom(event):
        raise RuntimeError("nope")

    release_events.accept_event = boom
    try:
        assert post(FIXTURE) == (500, "internal error")
    finally:
        release_events.accept_event = orig
    assert sender.delivered == 0


def test_health_is_not_delivery():
    assert get("/health") == (200, "ok")
    assert get_allowing_error("/missing")[0] == 404
    assert sender.calls == []


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
    global THREAD
    THREAD = threading.Thread(target=_serve, name="inbox-test", daemon=True)
    THREAD.start()
    assert READY.wait(5), BOX.get("error")
    if BOX.get("error"):
        raise BOX["error"]
    # Constructor binds before serve_forever; retry the first health check.
    deadline = time.time() + 5
    last = None
    while time.time() < deadline:
        try:
            last = get("/health")
            if last == (200, "ok"):
                break
        except Exception as exc:
            last = exc
        time.sleep(0.02)
    else:
        raise AssertionError(f"inbox did not become healthy: {last}")
    tests = [
        test_import_mode_post_delivers_once,
        test_auth,
        test_rejected_events_do_not_send,
        test_body_limits,
        test_duplicate_post,
        test_concurrent_posts,
        test_failed_delivery_then_retry,
        test_legacy_list_is_duplicate,
        test_corrupt_and_readonly,
        test_ready_booleans_do_not_call_sender,
        test_legacy_owned_http,
        test_handler_error_returns_500,
        test_health_is_not_delivery,
    ]
    try:
        results = [run(fn.__name__, fn) for fn in tests]
    finally:
        httpd = BOX.get("httpd")
        if httpd is not None:
            httpd.shutdown()
        THREAD.join(5)
    if all(results):
        print(f"\nAll tests passed. ({len(results)})")
        return 0
    print(f"\n{results.count(False)} failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
