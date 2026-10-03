"""Release Events v1 HTTP inbox.

The legacy poller is untouched. This module accepts producer events, dedupes
them, and delivers at most one Telegram message per id per process lifetime.

It is not exactly-once. A send can succeed and the process can die before
``delivered`` is written. Startup marks that leftover ``pending`` row
``unknown`` and does not send it again.
"""
from __future__ import annotations

import hmac
import html
import json
import os
import re
import tempfile
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bot

# Policy knobs from the receiver brief.
MAX_BODY = 65536
MAX_ID = 256
MAX_SHORT = 200
MAX_URL = 2048
MAX_SUMMARY = 4000
MAX_METADATA_BYTES = 8192
TELEGRAM_MAX = 4096
MAX_ATTEMPTS = 5
RETAIN_DAYS = 90
RETAIN_MIN_DELIVERED = 20000
LEGACY_BOUND = 20000
SCHEMA = "release-event/v1"
KINDS = {"software", "model"}
STATUSES = {"pending", "delivered", "failed", "unknown"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/@+-]*$")
DEFAULT_LEGACY_OWNED = (
    "openclaw/openclaw,"
    "nousresearch/hermes-agent,"
    "openai/codex,"
    "anthropics/claude-code,"
    "openclaw,"
    "hermes-agent,"
    "@openai/codex,"
    "@anthropic-ai/claude-code"
)
REQUIRED = ("schema", "id", "kind", "name", "version", "source", "url")

_LOCK = threading.Lock()
_MISSING = object()


def wire(bot_module) -> None:
    """Use this module's ``send_telegram``. ``runner.py`` calls this explicitly."""
    global bot
    if bot_module is None or not hasattr(bot_module, "send_telegram"):
        raise ValueError("bot module must provide send_telegram")
    bot = bot_module


def legacy_path() -> Path:
    raw = os.environ.get("RELEASE_EVENTS_STATE", "/data/release-events-seen.json")
    return Path(raw)


def v2_path() -> Path:
    """Ledger path. Never the legacy list path, so a v2 write cannot replace it."""
    legacy = legacy_path()
    explicit = os.environ.get("RELEASE_EVENTS_STATE_V2", "").strip()
    chosen = Path(explicit) if explicit else legacy.parent / "release-events-state.json"
    if _same_path(chosen, legacy):
        chosen = legacy.parent / "release-events-state.json"
    if _same_path(chosen, legacy):
        chosen = legacy.with_name(legacy.name + ".v2")
    return chosen


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(eid: str, outcome: str) -> None:
    print(f"[EVENTS] id={_log_id(eid)} outcome={outcome}", flush=True)


def _log_id(eid: object) -> str:
    if isinstance(eid, str) and len(eid) <= MAX_ID and ID_RE.fullmatch(eid):
        return eid
    return "-"


def _error(kind: str) -> None:
    # ``kind`` is a fixed word, never a body, header, or token.
    print(f"[EVENTS][ERROR] {kind}", flush=True)


def canonical_id(event) -> str:
    """Caller id only. Null, blank, and non-strings do not fall back to a hash."""
    if not isinstance(event, dict):
        return ""
    raw = event.get("id")
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def validate(event) -> str:
    """Return "" when the event may be delivered, else a short reason."""
    if not isinstance(event, dict):
        return "invalid body"
    for key in REQUIRED:
        reason = _required_text(event, key)
        if reason:
            return reason
    if event["schema"].strip() != SCHEMA:
        return "unsupported schema"
    if event["kind"].strip() not in KINDS:
        return "invalid kind"
    if len(event["id"].strip()) > MAX_ID or not ID_RE.fullmatch(event["id"].strip()):
        return "invalid id"
    for key in ("name", "version", "source"):
        if len(event[key].strip()) > MAX_SHORT:
            return f"invalid {key}"
    # Do not strip first: a trailing newline would disappear and look valid.
    url_reason = _validate_url(event["url"])
    if url_reason:
        return url_reason
    if "summary" in event:
        summary = event["summary"]
        if not isinstance(summary, str) or len(summary) > MAX_SUMMARY:
            return "invalid summary"
    if "metadata" in event:
        meta = event["metadata"]
        if not isinstance(meta, dict):
            return "invalid metadata"
        try:
            encoded = json.dumps(meta).encode("utf-8")
        except (TypeError, ValueError):
            return "invalid metadata"
        if len(encoded) > MAX_METADATA_BYTES:
            return "invalid metadata"
    return ""


def _required_text(event: dict, key: str) -> str:
    if key not in event or event[key] is None or not isinstance(event[key], str):
        if key not in event:
            return f"missing {key}"
        return f"invalid {key}"
    if not event[key].strip():
        return f"missing {key}"
    return ""


def _validate_url(url: str) -> str:
    # http is rejected. Only https, with a host, and no characters that break
    # an HTML attribute or the request line.
    if len(url) > MAX_URL:
        return "invalid url"
    for char in url:
        code = ord(char)
        if code < 32 or code == 127 or char.isspace() or char in "\"'":
            return "invalid url"
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "invalid url"
    if parts.scheme != "https" or not parts.hostname:
        return "invalid url"
    return ""


def format_event(event: dict) -> str:
    """Telegram HTML, at most 4096 characters, with quotes escaped."""
    icon = "🤖" if str(event.get("kind") or "").strip() == "model" else "🚀"
    name = str(event.get("name") or "").strip()
    version = str(event.get("version") or "").strip()
    summary = str(event.get("summary") or "").strip()[:MAX_SUMMARY]
    url = str(event.get("url") or "").strip()
    summary = _shrink(summary, lambda text: _compose(icon, name, version, text, url))
    message = _compose(icon, name, version, summary, url)
    if len(message) <= TELEGRAM_MAX:
        return message
    url = _shrink(url, lambda text: _compose(icon, name, version, "", text))
    message = _compose(icon, name, version, "", url)
    if len(message) <= TELEGRAM_MAX:
        return message
    name = _shrink(name, lambda text: _compose(icon, text, version, "", url), strip=False)
    message = _compose(icon, name, version, "", url)
    if len(message) <= TELEGRAM_MAX:
        return message
    version = _shrink(version, lambda text: _compose(icon, name, text, "", url), strip=False)
    return _compose(icon, name, version, "", url)


def _shrink(raw: str, build, strip: bool = True) -> str:
    if len(build(raw if not strip else raw.rstrip())) <= TELEGRAM_MAX:
        return raw.rstrip() if strip else raw
    best = 0
    low = 0
    high = len(raw)
    while low <= high:
        mid = (low + high) // 2
        if len(build(raw[:mid])) <= TELEGRAM_MAX:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    kept = raw[:best]
    return kept.rstrip() if strip else kept


def _compose(icon: str, name: str, version: str, summary: str, url: str) -> str:
    name_esc = html.escape(name, quote=True)
    version_esc = html.escape(version, quote=True)
    summary_esc = html.escape(summary, quote=True)
    url_esc = html.escape(url, quote=True)
    message = f"{icon} <b>{name_esc} {version_esc}</b>"
    if summary_esc:
        message += f"\n\n{summary_esc}"
    message += f'\n\n🔗 <a href="{url_esc}">Release</a>'
    return message


def html_to_plain(text: str) -> str:
    """Same tag-strip and entity decode as ``bot.send_telegram``'s plain retry."""
    plain = re.sub(r"<[^>]+>", "", text)
    plain = plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    if len(plain) > TELEGRAM_MAX:
        plain = plain[:TELEGRAM_MAX]
    return plain


def _deliver(text: str) -> bool:
    if bot.send_telegram(text, parse_mode="HTML"):
        return True
    # Production send_telegram already retries plain internally and returns
    # False only when that also failed. A sender that rejects HTML (tests, or
    # a substitute) gets one plain attempt here. A True from the first call
    # never reaches this line, so a successful HTML send is not repeated.
    return bool(bot.send_telegram(html_to_plain(text), parse_mode=None))


def _legacy_owned(event: dict) -> bool:
    meta = event.get("metadata")
    if not isinstance(meta, dict):
        return False
    owned = _owned_keys()
    for field in ("repo", "package"):
        value = meta.get(field)
        if isinstance(value, str) and value.strip().casefold() in owned:
            return True
    return False


def _owned_keys() -> set[str]:
    raw = os.environ.get("RELEASE_EVENTS_LEGACY_OWNED", DEFAULT_LEGACY_OWNED)
    return {part.strip().casefold() for part in raw.split(",") if part.strip()}


def _blank(attempts: int, status: str = "pending") -> dict:
    now = _now()
    return {
        "status": status,
        "attempts": attempts,
        "first_seen": now,
        "last_attempt": now,
        "delivered_at": now if status == "delivered" else None,
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        # The rename is already visible. A directory fsync failure must not
        # look like a missed write, or the id stays pending and is never sent.
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            return
    except Exception:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise


def _read_text(path: Path):
    if not path.exists():
        return _MISSING
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _valid_record(record) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("status") not in STATUSES:
        return False
    attempts = record.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        return False
    if not isinstance(record.get("first_seen"), str) or not isinstance(record.get("last_attempt"), str):
        return False
    delivered = record.get("delivered_at")
    if delivered is not None and not isinstance(delivered, str):
        return False
    return True


def _valid_ledger(data) -> bool:
    if not isinstance(data, dict) or data.get("version") != 2:
        return False
    events = data.get("events")
    if not isinstance(events, dict):
        return False
    return all(isinstance(eid, str) and _valid_record(rec) for eid, rec in events.items())


def _load(log: bool = True):
    """Return ``(ledger, error, dirty)``. Missing files are an empty ledger."""
    legacy_ids, legacy_err = _read_legacy()
    if legacy_err:
        if log:
            _error("state corrupt")
        return None, "corrupt", False
    text = _read_text(v2_path())
    dirty = False
    if text is _MISSING:
        ledger = {"version": 2, "events": {}}
    elif text is None:
        if log:
            _error("state corrupt")
        return None, "corrupt", False
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if log:
                _error("state corrupt")
            return None, "corrupt", False
        if not _valid_ledger(data):
            if log:
                _error("state corrupt")
            return None, "corrupt", False
        ledger = {"version": 2, "events": dict(data["events"])}
    for eid in legacy_ids:
        if eid not in ledger["events"]:
            ledger["events"][eid] = _blank(1, "delivered")
            dirty = True
    return ledger, None, dirty


def _read_legacy():
    text = _read_text(legacy_path())
    if text is _MISSING:
        return [], None
    if text is None:
        return None, "corrupt"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "corrupt"
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        return None, "corrupt"
    return list(data), None


def _stamp(record: dict) -> datetime:
    raw = record.get("delivered_at") or record.get("first_seen") or ""
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _retain(events: dict) -> dict:
    protected = {}
    delivered = []
    for eid, record in events.items():
        if record.get("status") == "delivered":
            delivered.append((eid, record))
        else:
            protected[eid] = record
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETAIN_DAYS)
    delivered.sort(key=lambda item: (_stamp(item[1]), item[0]))
    if len(delivered) > RETAIN_MIN_DELIVERED:
        newest = {eid for eid, _record in delivered[-RETAIN_MIN_DELIVERED:]}
        delivered = [
            (eid, record)
            for eid, record in delivered
            if eid in newest or _stamp(record) >= cutoff
        ]
    kept = dict(protected)
    for eid, record in delivered:
        kept[eid] = record
    return kept


def _save_v2(ledger: dict) -> None:
    payload = {"version": 2, "events": _retain(ledger["events"])}
    ledger["events"] = payload["events"]
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _atomic_write(v2_path(), text)


def _append_legacy(eid: str) -> None:
    ids, err = _read_legacy()
    if err:
        _error("state corrupt")
        return
    if eid in ids:
        return
    ids.append(eid)
    if len(ids) > LEGACY_BOUND:
        ids = ids[-LEGACY_BOUND:]
    _atomic_write(legacy_path(), json.dumps(ids) + "\n")


def _persist_migration(ledger: dict, dirty: bool) -> None:
    if not dirty:
        return
    try:
        _save_v2(ledger)
    except OSError:
        _error("state unwritable")


def reconcile_startup() -> None:
    """Mark crash leftovers ``unknown``. Do not send them."""
    with _LOCK:
        ledger, err, dirty = _load()
        if err or ledger is None:
            return
        changed = dirty
        for eid, record in ledger["events"].items():
            if record.get("status") == "pending":
                record["status"] = "unknown"
                changed = True
                _log(eid, "unknown")
        if not changed:
            return
        try:
            _save_v2(ledger)
        except OSError:
            _error("state unwritable")


def _reserve(eid: str):
    with _LOCK:
        ledger, err, dirty = _load()
        if err or ledger is None:
            raise OSError(err or "corrupt")
        _persist_migration(ledger, dirty)
        record = ledger["events"].get(eid)
        if record is not None and record["status"] in ("delivered", "unknown"):
            return "duplicate", record["attempts"]
        if record is not None and record["status"] == "pending":
            return "in_progress", record["attempts"]
        if record is not None and record["status"] == "failed" and record["attempts"] >= MAX_ATTEMPTS:
            return "gave_up", record["attempts"]
        if record is None:
            record = _blank(1)
            ledger["events"][eid] = record
        else:
            record["attempts"] = int(record["attempts"]) + 1
            record["status"] = "pending"
            record["last_attempt"] = _now()
            record["delivered_at"] = None
        try:
            _save_v2(ledger)
        except OSError:
            _error("state unwritable")
            raise
        return "send", record["attempts"]


def _finish(eid: str, ok: bool) -> int:
    with _LOCK:
        ledger, err, dirty = _load()
        if err or ledger is None:
            raise OSError(err or "corrupt")
        _persist_migration(ledger, dirty)
        record = ledger["events"].get(eid)
        if record is None:
            record = _blank(1)
            ledger["events"][eid] = record
        record["last_attempt"] = _now()
        if ok:
            record["status"] = "delivered"
            record["delivered_at"] = record["last_attempt"]
        else:
            record["status"] = "failed"
            record["delivered_at"] = None
        try:
            _save_v2(ledger)
        except OSError:
            _error("state unwritable")
            raise
        if ok:
            try:
                _append_legacy(eid)
            except OSError:
                # v2 already says delivered. A later rollback may not see this
                # id; do not send again to repair the legacy list.
                _error("state unwritable")
        return int(record["attempts"])


def accept_event(event) -> tuple[int, str]:
    if not isinstance(event, dict):
        _log("-", "rejected(invalid body)")
        return 400, "invalid body"
    reason = validate(event)
    if reason:
        _log(canonical_id(event), f"rejected({reason})")
        return 400, reason
    if _legacy_owned(event):
        _log(canonical_id(event), "owned_by_poller")
        return 200, "owned_by_poller"
    eid = canonical_id(event)
    try:
        action, attempts = _reserve(eid)
    except OSError:
        return 503, "state unavailable"
    if action == "duplicate":
        _log(eid, "duplicate")
        return 200, "duplicate"
    if action == "in_progress":
        _log(eid, "pending")
        return 202, "in_progress"
    if action == "gave_up":
        _log(eid, "gave_up")
        return 409, "gave_up"
    _log(eid, "received")
    try:
        ok = _deliver(format_event(event))
    except Exception as exc:
        _error(f"delivery error {type(exc).__name__}")
        ok = False
    try:
        attempts = _finish(eid, ok)
    except OSError:
        return 503, "state unavailable"
    if ok:
        _log(eid, "delivered")
        return 202, "accepted"
    _log(eid, "failed")
    if attempts >= MAX_ATTEMPTS:
        _log(eid, "gave_up")
        return 409, "gave_up"
    return 503, "delivery failed"


def _directory_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    name = None
    try:
        handle = tempfile.NamedTemporaryFile(
            dir=directory, prefix=".ready-", delete=False
        )
        name = handle.name
        handle.close()
        return True
    except OSError:
        return False
    finally:
        if name:
            try:
                os.unlink(name)
            except OSError:
                pass


def readiness() -> tuple[int, dict]:
    """Booleans only. No Telegram call and no secret values."""
    token_ok = bool(os.environ.get("RELEASE_EVENTS_TOKEN", "").strip())
    telegram_ok = bool(
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        and os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    )
    writable = _directory_writable(v2_path().parent) and _directory_writable(legacy_path().parent)
    if writable:
        _ledger, err, _dirty = _load(log=False)
        if err:
            writable = False
    body = {
        "token": token_ok,
        "telegram": telegram_ok,
        "state_writable": writable,
        "worker": True,
    }
    return (200 if all(body.values()) else 503), body


def _authorized(header: str) -> bool:
    token = os.environ.get("RELEASE_EVENTS_TOKEN", "")
    if not token or not header:
        return False
    try:
        return hmac.compare_digest(header, f"Bearer {token}")
    except (TypeError, ValueError):
        return False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self._get()
        except Exception as exc:
            _error(f"handler error {type(exc).__name__}")
            self._reply(500, "internal error")

    def do_POST(self):
        try:
            self._post()
        except Exception as exc:
            _error(f"handler error {type(exc).__name__}")
            self._reply(500, "internal error")

    def _get(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._reply(200, "ok")
            return
        if path == "/ready":
            code, body = readiness()
            self._reply(code, json.dumps(body), "application/json")
            return
        self._reply(404, "")

    def _post(self):
        if self.path.split("?", 1)[0] != "/v1/releases":
            self._reply(404, "")
            return
        if not _authorized(self.headers.get("Authorization", "")):
            self._reply(401, "")
            return
        media = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            self._reply(415, "unsupported media type")
            return
        if self.headers.get("Transfer-Encoding"):
            self._reply(411, "length required")
            return
        if "Content-Length" not in self.headers:
            self._reply(411, "length required")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            self._reply(400, "bad length")
            return
        if length < 1 or length > MAX_BODY:
            self._reply(400, "bad length")
            return
        try:
            raw = self.rfile.read(length)
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._reply(400, "invalid json")
            return
        if not isinstance(event, dict):
            self._reply(400, "invalid body")
            return
        code, message = accept_event(event)
        self._reply(code, message)

    def _reply(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        return


def serve(host: str | None = None, port: int | None = None, on_bind=None) -> None:
    bind_host = host if host is not None else "0.0.0.0"
    bind_port = int(os.environ.get("PORT", "8080")) if port is None else int(port)
    reconcile_startup()
    httpd = ThreadingHTTPServer((bind_host, bind_port), Handler)
    print(f"[START] release event inbox on :{httpd.server_address[1]}", flush=True)
    if on_bind is not None:
        on_bind(httpd)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
