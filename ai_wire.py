"""Best-effort AI Wire registry push for alerts that Telegram already accepted.

The flag defaults off. A push runs only after a successful send, times out
after 5 seconds, retries once, and never raises. The private chat has no
public message URL, so items omit channel_post_url.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

_ON = {"1", "true", "yes", "on"}
_ATTEMPTS = 2
_TIMEOUT = 5
_SUMMARY_MAX = 600


def _enabled() -> bool:
    return os.environ.get("AI_WIRE_ENABLED", "").strip().lower() in _ON


def _iso(value: str) -> str:
    raw = (value or "").strip()
    if not raw or raw.casefold() == "unknown":
        return ""
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return ""
    return raw


def _split_repo(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    if text.count("/") != 1 or any(ch.isspace() for ch in text):
        return "", ""
    owner, repo = text.split("/", 1)
    if not owner or not repo:
        return "", ""
    return owner, repo


def _npm_org(package: str) -> str:
    if package.startswith("@") and "/" in package:
        return package[1:].split("/", 1)[0]
    return ""


def _item(
    *,
    canonical_key: str,
    title: str,
    url: str,
    summary: str,
    published_at: str,
    org: str,
    package: str,
    version: str,
) -> dict:
    item = {
        "source_bot": "release-bot",
        "kind": "tool_release",
        "canonical_key": canonical_key.strip().lower(),
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "channel": "release-alerts",
    }
    text = (summary or "").strip()
    if text:
        item["summary"] = text[:_SUMMARY_MAX]
    published = _iso(published_at)
    if published:
        item["published_at"] = published
    owner = (org or "").strip()
    if owner:
        item["org"] = owner
    extra = {}
    if package:
        extra["package"] = package
    if version:
        extra["version"] = version
    if extra:
        item["extra"] = extra
    return item


def item_for_poll(
    *,
    package_name: str,
    version: str,
    title: str,
    summary: str,
    url: str,
    published_at: str = "",
    github_repo: str = "",
    github_tag: str = "",
) -> dict:
    """Map one polled npm/GitHub alert. A GitHub tag wins over the npm-only key."""
    package = (package_name or "").strip()
    ver = (version or "").strip()
    owner, repo = _split_repo(github_repo)
    tag = (github_tag or "").strip()
    if owner and repo and tag:
        key = f"release:{owner}/{repo}@{tag}"
        org = owner
    else:
        key = f"release:npm/{package}@{ver}"
        org = _npm_org(package)
    return _item(
        canonical_key=key,
        title=title,
        url=url,
        summary=summary,
        published_at=published_at,
        org=org,
        package=package,
        version=ver,
    )


def _github_release(url: str) -> tuple[str, str, str] | None:
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host != "github.com":
        return None
    segs = [urllib.parse.unquote(seg) for seg in parts.path.split("/") if seg]
    if len(segs) < 5 or segs[2] != "releases" or segs[3] != "tag":
        return None
    owner, repo, tag = segs[0], segs[1], "/".join(segs[4:])
    if not owner or not repo or not tag:
        return None
    return owner, repo, tag


def _npm_from_url(url: str) -> tuple[str, str] | None:
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in {"www.npmjs.com", "npmjs.com"}:
        return None
    segs = [urllib.parse.unquote(seg) for seg in parts.path.split("/") if seg]
    if len(segs) < 4 or segs[0] != "package" or segs[-2] != "v" or not segs[-1]:
        return None
    name = "/".join(segs[1:-2])
    if not name:
        return None
    return name, segs[-1]


def _url_key(url: str) -> str:
    raw = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        parts = None
    if not parts or not parts.scheme or not parts.netloc:
        return "url:" + raw.rstrip("/").lower()
    path = parts.path.rstrip("/")
    return f"url:{parts.scheme.lower()}://{parts.netloc.lower()}{path}".lower()


def item_for_event(event: dict, summary: str) -> dict:
    """Map one inbox event. ``summary`` is the plain text that went to Telegram."""
    event = event if isinstance(event, dict) else {}
    url = event.get("url") if isinstance(event.get("url"), str) else ""
    version = event.get("version") if isinstance(event.get("version"), str) else ""
    name = event.get("name") if isinstance(event.get("name"), str) else ""
    version = version.strip()
    name = name.strip()
    title = " ".join(part for part in (name, version) if part)
    meta = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    package = meta.get("package") if isinstance(meta.get("package"), str) else ""
    package = package.strip()
    published = ""
    for source in (event, meta):
        if not isinstance(source, dict):
            continue
        for key in ("published_at", "published"):
            raw = source.get(key)
            if isinstance(raw, str):
                published = _iso(raw)
            if published:
                break
        if published:
            break

    def emit(key: str, org: str, pkg: str, ver: str) -> dict:
        return _item(
            canonical_key=key,
            title=title,
            url=url,
            summary=summary,
            published_at=published,
            org=org,
            package=pkg,
            version=ver,
        )

    gh = _github_release(url)
    npm = _npm_from_url(url)
    if gh:
        owner, repo, tag = gh
        return emit(f"release:{owner}/{repo}@{tag}", owner, package, version)
    if npm:
        pkg = package or npm[0]
        ver = version or npm[1]
        return emit(f"release:npm/{pkg}@{ver}", _npm_org(pkg), pkg, ver)

    owner, repo = _split_repo(meta.get("repo") if isinstance(meta.get("repo"), str) else "")
    tag = meta.get("tag") if isinstance(meta.get("tag"), str) else ""
    tag = tag.strip() or version
    if owner and repo and tag:
        return emit(f"release:{owner}/{repo}@{tag}", owner, package, version)
    if package:
        return emit(f"release:npm/{package}@{version}", _npm_org(package), package, version)

    return _item(
        canonical_key=_url_key(url),
        title=title,
        url=url,
        summary=summary,
        published_at=published,
        org="",
        package=package,
        version=version,
    )


def _reason(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        cause = exc.reason
        if isinstance(cause, TimeoutError) or "timed out" in str(cause).lower():
            return "timeout"
        return "network"
    if isinstance(exc, ValueError) and str(exc) == "bad response":
        return "bad response"
    return type(exc).__name__


def _failed(reason: str) -> None:
    print(f"ai_wire push failed: {reason}", flush=True)


def _upserted(raw: bytes) -> int:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("bad response")
    count = data.get("upserted")
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("bad response")
    return count


def _post(url: str, body: bytes, token: str) -> int:
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "sovs-release-bot/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return _upserted(resp.read())


def push_event(event: dict, summary: str) -> None:
    """Map one delivered inbox alert and push it. No-op when the flag is off."""
    try:
        if not _enabled():
            return
        push_item(item_for_event(event, summary))
    except Exception as exc:
        _failed(_reason(exc))


def push_item(item: dict) -> None:
    """POST one item. No-op when the flag is off. Never raises."""
    try:
        if not isinstance(item, dict) or not _enabled():
            return
        base = os.environ.get("AI_WIRE_URL", "").strip().rstrip("/")
        token = os.environ.get("AI_WIRE_INGEST_TOKEN", "").strip()
        if not base or not token:
            _failed("not configured")
            return
        body = json.dumps(item).encode("utf-8")
        url = base + "/api/ingest/items"
        last = "request failed"
        for _attempt in range(_ATTEMPTS):
            try:
                count = _post(url, body, token)
            except Exception as exc:
                last = _reason(exc)
                continue
            print(f"ai_wire push ok n={count}", flush=True)
            return
        _failed(last)
    except Exception as exc:
        _failed(_reason(exc))
