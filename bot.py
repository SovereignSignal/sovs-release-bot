#!/usr/bin/env python3
"""
Sovs Release Bot — Telegram alerts for new OpenClaw, Hermes, Codex, and Claude Code versions.

Monitors npm registry and GitHub releases. Sends a Telegram message
with a summary of changes when a new version is published.

Usage:
    python3 bot.py              # Check once and exit
    python3 bot.py --daemon     # Run continuously (check every 30 min)
    python3 bot.py --status     # Print npm + GitHub vs stored baselines (no Telegram)
    python3 bot.py --test       # Send a test message with current version info
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

# ── Config ──────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))  # minutes
STATE_FILE = Path(os.environ.get("STATE_FILE", "/opt/sovs-release-bot/data/last-version.txt"))
STATE_DIR = Path(os.environ.get("STATE_DIR", str(STATE_FILE.parent)))
WATCH_PACKAGES = [p.strip() for p in os.environ.get("WATCH_PACKAGES", "openclaw,hermes-agent,codex,claude-code").split(",") if p.strip()]
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com/api").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "deepseek-v4.1-flash")
# Optional second model. Unset uses this default; set to empty to skip the retry.
OLLAMA_MODEL_FALLBACK = os.environ.get("OLLAMA_MODEL_FALLBACK", "glm-5.3-flash")
# Release summaries stay at or below 0.2 so wording does not drift from the notes.
OLLAMA_TEMPERATURE = 0.2


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


SUMMARY_TIMEOUT = _int_env("SUMMARY_TIMEOUT", 30)
GH_RELEASE_PAGE_SIZE = _int_env("GH_RELEASE_PAGE_SIZE", 15)

PACKAGE_CONFIG = {
    "openclaw": {
        "label": "OpenClaw",
        "emoji": "🦞",
        "npm": "openclaw",
        "github": "openclaw/openclaw",
        "docker": "ghcr.io/openclaw/openclaw:{version}",
        "github_tag_prefixes": ["v"],
        # npm `latest` has been frozen on 2026.7.x while new work ships as betas.
        "include_prereleases": True,
    },
    "hermes-agent": {
        "label": "Hermes Agent",
        "emoji": "🪽",
        "npm": "hermes-agent",
        "github": "NousResearch/hermes-agent",
        "github_tag_prefixes": ["v"],
        "include_prereleases": False,
    },
    "codex": {
        "label": "Codex",
        "emoji": "⌨️",
        "npm": "@openai/codex",
        "github": "openai/codex",
        "github_tag_prefixes": ["rust-v", "v"],
        "include_prereleases": False,
    },
    "claude-code": {
        "label": "Claude Code",
        "emoji": "✳️",
        "npm": "@anthropic-ai/claude-code",
        "github": "anthropics/claude-code",
        "github_tag_prefixes": ["v"],
        "include_prereleases": False,
    },
}

# ── Helpers ─────────────────────────────────────────────────────────

def fetch_json(url: str, timeout: int = 15, warn: bool = True) -> Any:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sovs-release-bot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        if warn:
            print(f"[WARN] Failed to fetch {url}: {e}")
        return None


def post_json(url: str, payload: dict, timeout: int = 15, headers: dict | None = None, warn: bool = True) -> dict | None:
    try:
        body = json.dumps(payload).encode()
        req_headers = {
            "Content-Type": "application/json",
            "User-Agent": "sovs-release-bot/1.0",
        }
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        if warn:
            print(f"[WARN] LLM summary unavailable: {type(e).__name__}")
        return None


def package_config(package_key: str) -> dict:
    config = dict(PACKAGE_CONFIG.get(package_key, {}))
    if not config:
        config = {"label": package_key, "emoji": "📦", "npm": package_key}
    config.setdefault("npm", package_key)
    config.setdefault("label", config["npm"])
    config.setdefault("emoji", "📦")
    config.setdefault("github_tag_prefixes", ["v"])
    config.setdefault("include_prereleases", False)
    return config


def watches_prereleases(package_key: str) -> bool:
    """WATCH_PRERELEASES overrides package defaults when set.

    unset  → use PACKAGE_CONFIG.include_prereleases
    all    → every watched package
    none   → no prereleases
    csv    → only those keys
    """
    raw = os.environ.get("WATCH_PRERELEASES")
    if raw is None:
        return bool(package_config(package_key).get("include_prereleases"))
    lowered = raw.strip().lower()
    if lowered in ("", "none", "false", "0"):
        return False
    if lowered == "all":
        return True
    return package_key in {p.strip() for p in raw.split(",") if p.strip()}


def normalize_version(value: str) -> str:
    if not value:
        return ""
    v = value.strip()
    lower = v.lower()
    for prefix in ("rust-v", "rust-", "v"):
        if lower.startswith(prefix):
            v = v[len(prefix):]
            break
    return v.strip()


def version_in_text(version: str, text: str) -> bool:
    """True if `version` appears in `text` as a full version token (not a prefix).

    Accepts an optional `v` / `rust-v` prefix so `0.20.6` matches
    `Hermes Agent v0.20.6 (v2026.8.27)` and `0.150.1` matches `rust-v0.150.1`,
    without treating `0.150.1` as a hit inside `rust-v0.150.10`.
    """
    nv = normalize_version(version)
    if not nv or not text:
        return False
    if normalize_version(text.strip()) == nv:
        return True
    return re.search(
        rf"(?<![\w.])(?:rust-v|v)?{re.escape(nv)}(?![\w.])",
        text,
        re.IGNORECASE,
    ) is not None


def release_matches_version(release: dict | None, version: str) -> bool:
    if not release or not version:
        return False
    blob = " ".join(filter(None, [release.get("tag_name") or "", release.get("name") or ""]))
    return version_in_text(version, blob)


def github_tag_candidates(package_key: str, version: str) -> list[str]:
    prefixes = package_config(package_key).get("github_tag_prefixes") or ["v"]
    tags: list[str] = []
    for prefix in prefixes:
        tags.append(f"{prefix}{version}")
    tags.append(version)
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def get_npm_latest(package_key: str) -> dict | None:
    config = package_config(package_key)
    package_name = config["npm"]
    data = fetch_json(f"https://registry.npmjs.org/{urllib.parse.quote(package_name, safe='@/')}")
    if not data or not isinstance(data, dict):
        return None
    version = data.get("dist-tags", {}).get("latest")
    if not version:
        return None
    published = data.get("time", {}).get(version, "unknown")
    repo = data.get("repository", "")
    if isinstance(repo, dict):
        repo = repo.get("url", "")
    return {
        "version": version,
        "published": published,
        "description": data.get("description", ""),
        "repository": repo,
        "package": package_name,
    }


def list_gh_releases(package_key: str, include_prereleases: bool, per_page: int | None = None) -> list[dict]:
    repo = package_config(package_key).get("github")
    if not repo:
        return []
    page_size = per_page or GH_RELEASE_PAGE_SIZE
    data = fetch_json(
        f"https://api.github.com/repos/{repo}/releases?per_page={page_size}",
        warn=False,
    )
    if not isinstance(data, list):
        return []
    out = []
    for rel in data:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        if rel.get("prerelease") and not include_prereleases:
            continue
        out.append(rel)
    return out


def newest_gh_release(package_key: str) -> dict | None:
    releases = list_gh_releases(package_key, include_prereleases=watches_prereleases(package_key))
    return releases[0] if releases else None


def get_gh_release(package_key: str, version: str) -> dict | None:
    """Get GitHub release for a version. Tries configured tag prefixes, then recent releases."""
    repo = package_config(package_key).get("github")
    if not repo or not version:
        return None

    for tag in github_tag_candidates(package_key, version):
        data = fetch_json(
            f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}",
            warn=False,
        )
        if isinstance(data, dict) and data.get("tag_name"):
            return data

    # Official Hermes tags are date-based (v2026.8.27) while npm is semver (0.20.6).
    for rel in list_gh_releases(package_key, include_prereleases=True):
        if release_matches_version(rel, version):
            return rel

    return None


def npm_info_from_github(package_key: str, gh_release: dict, npm_info: dict | None) -> dict:
    """Build the npm_info-shaped dict used by format_release_message for a GitHub-only alert."""
    config = package_config(package_key)
    tag = gh_release.get("tag_name") or ""
    version = normalize_version(tag) or tag
    name = gh_release.get("name") or ""
    # Prefer a semver in the title when the tag is a calendar version (Hermes).
    semver = re.search(r"\bv?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)\b", name)
    if semver and not re.match(r"^\d{4}\.\d{1,2}\.\d", semver.group(1)):
        version = semver.group(1)
    return {
        "version": version,
        "published": gh_release.get("published_at") or (npm_info or {}).get("published") or "unknown",
        "description": (npm_info or {}).get("description") or "",
        "repository": f"https://github.com/{config['github']}" if config.get("github") else "",
        "package": (npm_info or {}).get("package") or config["npm"],
    }


def parse_changelog(body: str) -> dict:
    """
    Parse GitHub release body into structured changelog.
    Returns: { "summary": str, "features": [], "fixes": [], "other": [], "contributors": [] }
    """
    result = {
        "summary": "",
        "features": [],
        "fixes": [],
        "breaking": [],
        "other": [],
        "contributors": [],
        "pr_count": 0,
        "excerpt": [],
    }

    if not body:
        return result

    # Extract "What's Changed" section (Claude Code uses lowercase "changed")
    changes_match = re.search(
        r"## What's Changed\s*\n(.*?)(?=\n## |\n\*\*Full Changelog|$)",
        body,
        re.DOTALL | re.IGNORECASE,
    )
    changes_text = changes_match.group(1) if changes_match else body

    pr_pattern = re.compile(
        r"\*\s*(.+?)\s+by\s+@(\S+)\s+in\s+https://github\.com/\S+/pull/(\d+)",
        re.MULTILINE,
    )

    contributors = set()
    for match in pr_pattern.finditer(changes_text):
        title = match.group(1).strip()
        author = match.group(2)
        pr_num = match.group(3)
        contributors.add(author)
        result["pr_count"] += 1

        title_lower = title.lower()
        clean_title = re.sub(r"^(feat|fix|chore|docs|refactor|perf|test|ci|build|style)\([^)]*\):\s*", "", title, flags=re.IGNORECASE)
        clean_title = re.sub(r"^(feat|fix|chore|docs|refactor|perf|test|ci|build|style):\s*", "", clean_title, flags=re.IGNORECASE)

        entry = f"{clean_title} (#{pr_num})"

        if "breaking" in title_lower or "BREAKING" in title:
            result["breaking"].append(entry)
        elif title_lower.startswith("feat"):
            result["features"].append(entry)
        elif title_lower.startswith("fix"):
            result["fixes"].append(entry)
        elif title_lower.startswith(("perf", "refactor")):
            result["other"].append(entry)
        else:
            result["other"].append(entry)

    result["contributors"] = sorted(contributors)

    important = re.search(r"^(Important:.*?)(?=\n##|\n\*\s)", body, re.DOTALL)
    if important:
        result["summary"] = important.group(1).strip()[:200]

    if not result["features"] and not result["fixes"] and not result["other"] and not result["breaking"]:
        result["excerpt"] = extract_plain_excerpt(body)

    return result


def extract_plain_excerpt(body: str, limit: int = 4) -> list[str]:
    """Pull a few useful lines from free-form release notes (Hermes, Codex, OpenClaw)."""
    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") or set(line) <= {"-", "=", "*"}:
            continue
        if line.lower().startswith("full changelog"):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^>\s+", "", line)
        line = line.replace("**", "")
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line[:220])
        if len(lines) >= limit:
            break
    return lines


# Version and integer tokens. Boundaries keep 1.2.3 from matching inside
# 1.2.30 or 11.2.3. A sentence period after a token is allowed; a dotted or
# prerelease continuation is not. v / rust-v prefixes share a core with the
# bare number so "v1.2.3" and "1.2.3" are the same fact.
# Prerelease suffixes stop before a sentence period ("1.2.3-beta.").
_VERSION_SUFFIX = r"[-+][0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*"
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![\w.])(?:"
    rf"(?P<prefix>rust-v|v)(?P<prefixed>\d+(?:\.\d+)*(?:{_VERSION_SUFFIX})*)"
    rf"|(?P<dotted>\d+(?:\.\d+)+(?:{_VERSION_SUFFIX})*)"
    r"|(?P<integer>\d+)"
    r")(?![\w]|[.](?=\d))",
    re.IGNORECASE,
)


def _summary_number_tokens(text: str) -> list[tuple[str, str, str]]:
    """Return (raw, kind, core) for number and version tokens, in order."""
    found: list[tuple[str, str, str]] = []
    for match in _NUMBER_TOKEN_RE.finditer(text or ""):
        if match.group("prefixed") is not None:
            found.append((match.group(0), "version", match.group("prefixed")))
        elif match.group("dotted") is not None:
            found.append((match.group(0), "version", match.group("dotted")))
        else:
            found.append((match.group(0), "integer", match.group("integer")))
    return found


def ungrounded_summary_numbers(summary: str, source: str) -> list[str]:
    """Number/version strings in `summary` that do not appear in `source`.

    Comparison is on the numeric core, so an optional v / rust-v prefix does
    not by itself count as a different fact. Components of a version are not
    treated as standalone integers: "3" is not grounded by "1.2.3".
    """
    source_versions: set[str] = set()
    source_integers: set[str] = set()
    for _raw, kind, core in _summary_number_tokens(source):
        if kind == "version":
            source_versions.add(core)
        else:
            source_integers.add(core)

    allowed_versions = source_versions | source_integers
    allowed_integers = source_integers | {core for core in source_versions if core.isdigit()}

    bad: list[str] = []
    seen: set[str] = set()
    for raw, kind, core in _summary_number_tokens(summary):
        allowed = allowed_versions if kind == "version" else allowed_integers
        if core in allowed or raw in seen:
            continue
        seen.add(raw)
        bad.append(raw)
    return bad


def release_summary_prompt(label: str, source_parts: list[str]) -> str:
    """Facts-only prompt. The instruction text itself contains no numbers."""
    return (
        f"Summarize this {label} release for a Telegram product update.\n"
        "Write concise bullets under the heading \"What's new\".\n"
        "Use only facts stated in the source text below. "
        "Copy versions and numbers verbatim. "
        "Keep the source's verbs. "
        "Do not add hype or opinion words. "
        "Do not speculate about impact.\n\n"
        + "\n\n".join(source_parts)
    )


def _model_reply_text(data: dict | None) -> str:
    """Text we would send. Reasoning-only payloads count as empty."""
    if not isinstance(data, dict):
        return ""
    raw = data.get("response")
    if not isinstance(raw, str):
        return ""
    lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
    return "\n".join(lines[:6])[:1200]


def _generate_summary(model: str, prompt: str) -> str:
    """Call /api/generate once. Empty string means timeout, HTTP error, or no text."""
    data = post_json(
        f"{OLLAMA_BASE_URL}/generate",
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": OLLAMA_TEMPERATURE},
        },
        timeout=SUMMARY_TIMEOUT,
        headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"},
        warn=False,
    )
    if data is None:
        print(f"[WARN] Ollama model {model} request failed")
        return ""
    text = _model_reply_text(data)
    if not text:
        print(f"[WARN] Ollama model {model} returned an empty reply")
        return ""
    return text


def _accept_summary(model: str, text: str, fact_text: str) -> str:
    """Return `text` when every number is in the source; otherwise empty."""
    bad = ungrounded_summary_numbers(text, fact_text)
    if bad:
        shown = ", ".join(bad[:8])
        print(f"[WARN] Ollama model {model} summary has number(s) not in the source: {shown}")
        return ""
    print(f"[OK] Release summary answered by {model}")
    return text


def summarize_release(package_key: str, npm_info: dict, gh_release: dict | None) -> str:
    """Return an optional LLM summary. Empty string means use normal changelog fallback.

    Tries OLLAMA_MODEL, then OLLAMA_MODEL_FALLBACK once, when the primary times
    out, returns an HTTP error, replies with no summary text, or includes a
    number/version that is not in the source. If both attempts fail, the caller
    sends the alert with the parsed changelog only.
    """
    if not OLLAMA_API_KEY:
        return ""

    body = ((gh_release or {}).get("body") or "").strip()
    description = (npm_info.get("description") or "").strip()
    if not body and not description:
        return ""

    clipped = body[:6000]
    source_parts = []
    if description:
        source_parts.append(f"npm description:\n{description}")
    if clipped:
        source_parts.append(f"GitHub release notes:\n{clipped}")
    fact_text = "\n".join(part for part in (description, clipped) if part)
    prompt = release_summary_prompt(package_config(package_key)["label"], source_parts)

    primary = (OLLAMA_MODEL or "").strip()
    fallback = (OLLAMA_MODEL_FALLBACK or "").strip()
    # At most two calls. A fallback that names the same model still retries once.
    models: list[str] = []
    if primary:
        models.append(primary)
    if fallback:
        models.append(fallback)

    saw_ungrounded = False
    for model in models:
        text = _generate_summary(model, prompt)
        if not text:
            continue
        kept = _accept_summary(model, text, fact_text)
        if kept:
            return kept
        saw_ungrounded = True

    if saw_ungrounded:
        print("[WARN] Dropping release summary")
    return ""


def format_release_message(package_key: str, npm_info: dict, gh_release: dict | None) -> str:
    """Format the release alert message with changelog summary."""
    config = package_config(package_key)
    version = npm_info["version"]
    published = npm_info["published"]
    package_name = npm_info.get("package") or config["npm"]
    repo = config.get("github")
    gh_url = gh_release.get("html_url") if gh_release else ""
    repo_url = f"https://github.com/{repo}" if repo else ""
    docker_tag = config.get("docker", "").format(version=version) if config.get("docker") else ""
    prerelease = bool(gh_release and gh_release.get("prerelease"))

    msg = f"{config['emoji']} <b>{_esc(config['label'])} {version}</b>"
    if prerelease:
        msg += " <i>(pre-release)</i>"
    msg += "\n\n"
    if npm_info.get("description"):
        msg += f"{_esc(npm_info['description'])}\n\n"

    llm_summary = summarize_release(package_key, npm_info, gh_release)
    if llm_summary:
        msg += f"{md_to_telegram_html(llm_summary)}\n\n"

    body = gh_release.get("body", "") if gh_release else ""
    changelog = parse_changelog(body)

    if changelog["summary"]:
        msg += f"ℹ️ {_esc(changelog['summary'])}\n\n"

    if changelog["breaking"]:
        msg += "🚨 <b>Breaking:</b>\n"
        for item in changelog["breaking"][:5]:
            msg += f"  • {_esc(item)}\n"
        msg += "\n"

    if changelog["features"]:
        msg += "✨ <b>Features:</b>\n"
        for item in changelog["features"][:5]:
            msg += f"  • {_esc(item)}\n"
        if len(changelog["features"]) > 5:
            msg += f"  <i>...and {len(changelog['features']) - 5} more</i>\n"
        msg += "\n"

    if changelog["fixes"]:
        msg += "🔧 <b>Fixes:</b>\n"
        for item in changelog["fixes"][:5]:
            msg += f"  • {_esc(item)}\n"
        if len(changelog["fixes"]) > 5:
            msg += f"  <i>...and {len(changelog['fixes']) - 5} more</i>\n"
        msg += "\n"

    if changelog["other"]:
        if len(changelog["other"]) <= 3:
            msg += "📦 <b>Other:</b>\n"
            for item in changelog["other"]:
                msg += f"  • {_esc(item)}\n"
            msg += "\n"
        else:
            msg += f"📦 +{len(changelog['other'])} other changes\n\n"
    elif changelog.get("excerpt") and not llm_summary:
        msg += "📝 <b>Notes:</b>\n"
        for item in changelog["excerpt"][:4]:
            msg += f"  • {_esc(item)}\n"
        msg += "\n"

    stats = []
    if changelog["pr_count"]:
        stats.append(f"{changelog['pr_count']} PRs")
    if changelog["contributors"]:
        stats.append(f"{len(changelog['contributors'])} contributors")
    if stats:
        msg += f"📊 {' · '.join(stats)}\n"

    links = [f"<a href=\"https://www.npmjs.com/package/{_url_package(package_name)}/v/{version}\">npm</a>"]
    if gh_url:
        links.insert(0, f"<a href=\"{gh_url}\">Release Notes</a>")
    elif repo_url:
        links.append(f"<a href=\"{repo_url}\">GitHub</a>")
    msg = msg.rstrip() + "\n\n"
    msg += f"📦 Published: {published}\n"
    if docker_tag:
        msg += f"🐳 <code>{docker_tag}</code>\n"
    msg += f"🔗 {' · '.join(links)}"

    return msg


def _url_package(package_name: str) -> str:
    return urllib.parse.quote(package_name, safe="@/")


def _esc(text: str) -> str:
    """Escape HTML entities for Telegram."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def md_to_telegram_html(text: str) -> str:
    """Convert a small Markdown subset to Telegram-safe HTML.

    Messages are sent with parse_mode=HTML, but the optional LLM summary comes
    back as Markdown. Without this, markers render literally (**bold**, `code`,
    "- " bullets, "# " headings). We escape HTML first, then map the common
    markers to tags. Italics are intentionally NOT supported: single * / _ would
    mangle snake_case identifiers and glob patterns common in release notes.
    """
    lines = []
    for raw in text.splitlines():
        line = raw.rstrip()

        heading = re.match(r"\s*#{1,6}\s+(.*)", line)
        if heading:
            line = heading.group(1)

        bullet = re.match(r"\s*[-*+]\s+(.*)", line)
        if bullet:
            line = bullet.group(1)

        line = _esc(line)

        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"__([^_]+)__", r"<b>\1</b>", line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', line)

        if heading:
            line = f"<b>{line}</b>"
        if bullet:
            line = f"• {line}"

        lines.append(line)

    html = "\n".join(lines)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def state_file_for(package_key: str) -> Path:
    # Backward-compatible: the deployed bot already stores OpenClaw in STATE_FILE.
    if package_key == "openclaw":
        return STATE_FILE
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", package_key).strip("-")
    return STATE_DIR / f"last-version-{safe}.txt"


def empty_state() -> dict:
    return {"npm": "", "github_tag": ""}


def load_state(package_key: str) -> dict:
    """Read stored baselines. Old plaintext files are treated as npm-only state."""
    try:
        text = state_file_for(package_key).read_text().strip()
    except FileNotFoundError:
        return empty_state()
    if not text:
        return empty_state()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return {
                    "npm": str(data.get("npm") or "").strip(),
                    "github_tag": str(data.get("github_tag") or "").strip(),
                }
        except json.JSONDecodeError:
            pass
    return {"npm": text, "github_tag": ""}


def save_state(package_key: str, state: dict):
    path = state_file_for(package_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "npm": (state.get("npm") or "").strip(),
        "github_tag": (state.get("github_tag") or "").strip(),
    }
    path.write_text(json.dumps(payload) + "\n")


def get_stored_version(package_key: str = "openclaw") -> str:
    state = load_state(package_key)
    return state["npm"] or state["github_tag"]


# ── Telegram ────────────────────────────────────────────────────────

def send_telegram(text: str, parse_mode: str | None = "HTML") -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] Telegram credentials missing; message not sent")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    def _post(payload_text: str, mode: str | None) -> dict | None:
        fields = {
            "chat_id": CHAT_ID,
            "text": payload_text,
            "disable_web_page_preview": "true",
        }
        if mode:
            fields["parse_mode"] = mode
        payload = urllib.parse.urlencode(fields).encode()
        try:
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            print(f"[ERROR] Failed to send Telegram message: {e}")
            return None

    result = _post(text, parse_mode)
    if result and result.get("ok"):
        print("[OK] Telegram message sent")
        return True

    if parse_mode:
        print("[WARN] Telegram HTML send failed, retrying as plain text")
        plain = re.sub(r"<[^>]+>", "", text)
        plain = plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        return send_telegram(plain, parse_mode=None)

    print(f"[ERROR] Telegram API: {result}")
    return False


# ── Main ────────────────────────────────────────────────────────────

def _sync_matching_channel(state: dict, npm_version: str, gh_release: dict | None) -> dict | None:
    """If a newly seen GitHub tag is the same release we already announced via npm, just record it."""
    if not gh_release:
        return None
    gh_tag = gh_release.get("tag_name") or ""
    if not gh_tag or gh_tag == state.get("github_tag"):
        return None
    known = state.get("npm") or npm_version
    if known and release_matches_version(gh_release, known):
        return {"npm": state.get("npm") or npm_version, "github_tag": gh_tag}
    if npm_version and release_matches_version(gh_release, npm_version) and npm_version == state.get("npm"):
        return {"npm": npm_version, "github_tag": gh_tag}
    return None


def check_package(package_key: str) -> bool:
    npm_info = get_npm_latest(package_key)
    gh_newest = newest_gh_release(package_key)
    state = load_state(package_key)

    npm_version = (npm_info or {}).get("version") or ""
    gh_tag = (gh_newest or {}).get("tag_name") or ""

    print(
        f"[CHECK] {package_key}: npm={npm_version or '-'} gh={gh_tag or '-'} "
        f"stored_npm={state['npm'] or '-'} stored_gh={state['github_tag'] or '-'} "
        f"prereleases={'on' if watches_prereleases(package_key) else 'off'}"
    )

    if not npm_info and not gh_newest:
        print(f"[WARN] Could not fetch npm registry or GitHub releases for {package_key}")
        return False

    if not state["npm"] and not state["github_tag"]:
        print(f"[INIT] First run for {package_key}, storing npm={npm_version or '-'} gh={gh_tag or '-'}")
        save_state(package_key, {"npm": npm_version, "github_tag": gh_tag})
        return False

    npm_new = bool(npm_version and npm_version != state["npm"])
    gh_new = bool(gh_tag and gh_tag != state["github_tag"])

    if gh_new and not npm_new:
        synced = _sync_matching_channel(state, npm_version, gh_newest)
        if synced:
            print(f"[SYNC] {package_key}: recorded GitHub tag {synced['github_tag']} (same release as npm {synced['npm']})")
            save_state(package_key, synced)
            return False

    if not npm_new and not gh_new:
        return False

    sent_any = False
    new_state = dict(state)

    if npm_new and npm_info:
        print(f"[NEW] {package_key} npm: {state['npm'] or '-'} → {npm_version}")
        gh_for_msg = get_gh_release(package_key, npm_version)
        if not gh_for_msg and gh_newest and release_matches_version(gh_newest, npm_version):
            gh_for_msg = gh_newest
        if send_telegram(format_release_message(package_key, npm_info, gh_for_msg)):
            new_state["npm"] = npm_version
            if gh_for_msg and gh_for_msg.get("tag_name"):
                new_state["github_tag"] = gh_for_msg["tag_name"]
            sent_any = True
        else:
            print(f"[WARN] {package_key}: Telegram send failed for npm {npm_version}; will retry next cycle")
            return sent_any

    # If npm and GitHub moved together to the same release, the npm send already covered it.
    if gh_new and gh_newest:
        already_announced = release_matches_version(gh_newest, new_state.get("npm") or "")
        if already_announced:
            new_state["github_tag"] = gh_tag
        elif not npm_new or not release_matches_version(gh_newest, npm_version):
            print(f"[NEW] {package_key} github: {state['github_tag'] or '-'} → {gh_tag}")
            info = npm_info_from_github(package_key, gh_newest, npm_info)
            if send_telegram(format_release_message(package_key, info, gh_newest)):
                new_state["github_tag"] = gh_tag
                sent_any = True
            else:
                print(f"[WARN] {package_key}: Telegram send failed for {gh_tag}; will retry next cycle")
                save_state(package_key, new_state)
                return sent_any
        else:
            new_state["github_tag"] = gh_tag

    save_state(package_key, new_state)
    return sent_any


def check_once() -> bool:
    found = False
    for package_key in WATCH_PACKAGES:
        found = check_package(package_key) or found
    return found


def print_status():
    print(f"Watching: {', '.join(WATCH_PACKAGES)}")
    print(f"{'package':<14} {'npm latest':<22} {'github':<28} {'stored npm':<22} {'stored gh':<28} alert?")
    print("-" * 130)
    for package_key in WATCH_PACKAGES:
        npm_info = get_npm_latest(package_key)
        gh = newest_gh_release(package_key)
        state = load_state(package_key)
        npm_version = (npm_info or {}).get("version") or "-"
        gh_tag = (gh or {}).get("tag_name") or "-"
        npm_new = bool(npm_info and npm_info["version"] != state["npm"] and state["npm"])
        gh_new = bool(gh and gh.get("tag_name") != state["github_tag"] and (state["npm"] or state["github_tag"]))
        if gh_new and gh and release_matches_version(gh, state["npm"] or ""):
            gh_new = False
        alert = "yes" if (npm_new or gh_new) else ("init" if not state["npm"] and not state["github_tag"] else "no")
        pre = " (pre)" if watches_prereleases(package_key) else ""
        print(
            f"{package_key:<14} {npm_version:<22} {(gh_tag + pre):<28} "
            f"{(state['npm'] or '-'):<22} {(state['github_tag'] or '-'):<28} {alert}"
        )


def test_message(package_key: str = "openclaw"):
    npm_info = get_npm_latest(package_key)
    gh_release = newest_gh_release(package_key)
    if npm_info:
        matched = get_gh_release(package_key, npm_info["version"]) or (
            gh_release if gh_release and release_matches_version(gh_release, npm_info["version"]) else None
        )
        msg = format_release_message(package_key, npm_info, matched or gh_release)
    elif gh_release:
        msg = format_release_message(package_key, npm_info_from_github(package_key, gh_release, None), gh_release)
    else:
        print(f"ERROR: Could not fetch npm registry or GitHub for {package_key}")
        sys.exit(1)

    msg = "🧪 <b>TEST — Release Bot Preview</b>\n\n" + msg
    if not send_telegram(msg):
        print(f"ERROR: Test message failed for {package_key}")
        sys.exit(1)
    version = npm_info["version"] if npm_info else (gh_release or {}).get("tag_name", "?")
    print(f"Test message sent for {package_key} {version}")


def daemon():
    print(f"[START] Sovs Release Bot — checking {', '.join(WATCH_PACKAGES)} every {CHECK_INTERVAL}min")
    print_status()

    while True:
        try:
            check_once()
        except Exception as e:
            print(f"[ERROR] Check failed: {e}")
        time.sleep(CHECK_INTERVAL * 60)


def _cli_packages() -> list[str]:
    if "--package" not in sys.argv:
        return ["openclaw"]
    try:
        value = sys.argv[sys.argv.index("--package") + 1]
    except IndexError:
        print("ERROR: --package requires a package key or 'all'")
        sys.exit(1)
    if value == "all":
        return list(WATCH_PACKAGES)
    return [value]


def main():
    if "--status" in sys.argv:
        print_status()
        return

    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID not set")
        sys.exit(1)

    if "--daemon" in sys.argv:
        daemon()
    elif "--test" in sys.argv:
        for package_key in _cli_packages():
            test_message(package_key)
    else:
        found = check_once()
        if not found:
            versions = ", ".join(f"{p} {get_stored_version(p) or '(checking...)'}" for p in WATCH_PACKAGES)
            print(f"[OK] No new version. Latest: {versions}")


if __name__ == "__main__":
    main()
