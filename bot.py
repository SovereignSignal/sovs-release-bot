#!/usr/bin/env python3
"""
Sovs Release Bot — Telegram alerts for new OpenClaw, Hermes, Codex, and Claude Code versions.

Monitors npm registry and GitHub releases. Sends a Telegram message
with a summary of changes when a new version is published.

Usage:
    python3 bot.py              # Check once and exit
    python3 bot.py --daemon     # Run continuously (check every 30 min)
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
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))  # minutes
STATE_FILE = Path(os.environ.get("STATE_FILE", "/opt/sovs-release-bot/data/last-version.txt"))
STATE_DIR = Path(os.environ.get("STATE_DIR", str(STATE_FILE.parent)))
WATCH_PACKAGES = [p.strip() for p in os.environ.get("WATCH_PACKAGES", "openclaw,hermes-agent,codex,claude-code").split(",") if p.strip()]
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com/api").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


SUMMARY_TIMEOUT = _int_env("SUMMARY_TIMEOUT", 30)

PACKAGE_CONFIG = {
    "openclaw": {
        "label": "OpenClaw",
        "emoji": "🦞",
        "npm": "openclaw",
        "github": "openclaw/openclaw",
        "docker": "ghcr.io/openclaw/openclaw:{version}",
    },
    "hermes-agent": {
        "label": "Hermes Agent",
        "emoji": "🪽",
        "npm": "hermes-agent",
        "github": "wyrtensi/hermes-agent-npm",
    },
    "codex": {
        "label": "Codex",
        "emoji": "⌨️",
        "npm": "@openai/codex",
        "github": "openai/codex",
    },
    "claude-code": {
        "label": "Claude Code",
        "emoji": "✳️",
        "npm": "@anthropic-ai/claude-code",
        "github": "anthropics/claude-code",
    },
}

# ── Helpers ─────────────────────────────────────────────────────────

def fetch_json(url: str, timeout: int = 15, warn: bool = True) -> dict | None:
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
    return config


def get_npm_latest(package_key: str) -> dict | None:
    config = package_config(package_key)
    package_name = config["npm"]
    data = fetch_json(f"https://registry.npmjs.org/{urllib.parse.quote(package_name, safe='@/')}")
    if not data:
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


def get_gh_release(package_key: str, version: str) -> dict | None:
    """Get GitHub release for a version. Tries exact tag, then with -N suffix."""
    repo = package_config(package_key).get("github")
    if not repo:
        return None
    # Try exact tag first
    data = fetch_json(f"https://api.github.com/repos/{repo}/releases/tags/v{version}", warn=False)
    if data and data.get("body"):
        return data

    # Try latest release (might have -1 suffix like v2026.3.13-1)
    data = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest", warn=False)
    if data and version in data.get("tag_name", ""):
        return data

    return data


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
    }

    if not body:
        return result

    # Extract "What's Changed" section
    changes_match = re.search(r"## What's Changed\s*\n(.*?)(?=\n## |\n\*\*Full Changelog|$)", body, re.DOTALL)
    if not changes_match:
        # Try without header
        changes_match = re.search(r"\* .+?(?:by @|$)", body, re.DOTALL)

    changes_text = changes_match.group(1) if changes_match else body

    # Parse individual PR lines: "* fix(thing): description by @user in https://..."
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

        # Classify by conventional commit prefix
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

    # Extract any important note at the top (before What's Changed)
    important = re.search(r"^(Important:.*?)(?=\n##|\n\*\s)", body, re.DOTALL)
    if important:
        result["summary"] = important.group(1).strip()[:200]

    return result


def summarize_release(package_key: str, npm_info: dict, gh_release: dict | None) -> str:
    """Return an optional LLM summary. Empty string means use normal changelog fallback."""
    if not OLLAMA_API_KEY:
        return ""

    body = (gh_release or {}).get("body", "").strip()
    description = (npm_info.get("description") or "").strip()
    if not body and not description:
        return ""

    config = package_config(package_key)
    source_parts = []
    if description:
        source_parts.append(f"npm description:\n{description}")
    if body:
        source_parts.append(f"GitHub release notes:\n{body[:6000]}")

    prompt = (
        f"Summarize this {config['label']} release for a Telegram product update.\n"
        "Write 2-4 concise bullets under the heading \"What's new\". "
        "Focus on user-visible changes, fixes, and upgrade-relevant notes. "
        "Do not invent details.\n\n"
        + "\n\n".join(source_parts)
    )

    data = post_json(
        f"{OLLAMA_BASE_URL}/generate",
        {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        },
        timeout=SUMMARY_TIMEOUT,
        headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"},
    )
    if not data:
        return ""

    summary = (data.get("response") or "").strip()
    if not summary:
        return ""

    # Keep Telegram alerts compact even if the model ignores the prompt.
    lines = [line.rstrip() for line in summary.splitlines() if line.strip()]
    return "\n".join(lines[:6])[:1200]


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

    # Header
    msg = f"{config['emoji']} <b>{_esc(config['label'])} {version}</b>\n\n"
    if npm_info.get("description"):
        msg += f"{_esc(npm_info['description'])}\n\n"

    llm_summary = summarize_release(package_key, npm_info, gh_release)
    if llm_summary:
        # The summary is Markdown from the LLM; render it as Telegram HTML so
        # markers don't show up literally. (Do NOT _esc() here — the converter
        # escapes internally and emits its own tags.)
        msg += f"{md_to_telegram_html(llm_summary)}\n\n"

    # Parse changelog
    body = gh_release.get("body", "") if gh_release else ""
    changelog = parse_changelog(body)

    # Summary line
    if changelog["summary"]:
        msg += f"ℹ️ {_esc(changelog['summary'])}\n\n"

    # Breaking changes (always show)
    if changelog["breaking"]:
        msg += "🚨 <b>Breaking:</b>\n"
        for item in changelog["breaking"][:5]:
            msg += f"  • {_esc(item)}\n"
        msg += "\n"

    # Features (top 5)
    if changelog["features"]:
        msg += "✨ <b>Features:</b>\n"
        for item in changelog["features"][:5]:
            msg += f"  • {_esc(item)}\n"
        if len(changelog["features"]) > 5:
            msg += f"  <i>...and {len(changelog['features']) - 5} more</i>\n"
        msg += "\n"

    # Fixes (top 5)
    if changelog["fixes"]:
        msg += "🔧 <b>Fixes:</b>\n"
        for item in changelog["fixes"][:5]:
            msg += f"  • {_esc(item)}\n"
        if len(changelog["fixes"]) > 5:
            msg += f"  <i>...and {len(changelog['fixes']) - 5} more</i>\n"
        msg += "\n"

    # Other changes (summarized count only if many)
    if changelog["other"]:
        if len(changelog["other"]) <= 3:
            msg += "📦 <b>Other:</b>\n"
            for item in changelog["other"]:
                msg += f"  • {_esc(item)}\n"
            msg += "\n"
        else:
            msg += f"📦 +{len(changelog['other'])} other changes\n\n"

    # Stats line
    stats = []
    if changelog["pr_count"]:
        stats.append(f"{changelog['pr_count']} PRs")
    if changelog["contributors"]:
        stats.append(f"{len(changelog['contributors'])} contributors")
    if stats:
        msg += f"📊 {' · '.join(stats)}\n"

    # Links
    links = [f"<a href=\"https://www.npmjs.com/package/{_url_package(package_name)}/v/{version}\">npm</a>"]
    if gh_url:
        links.insert(0, f"<a href=\"{gh_url}\">Release Notes</a>")
    elif repo_url:
        links.append(f"<a href=\"{repo_url}\">GitHub</a>")
    # Normalize spacing: exactly one blank line between the body and the footer,
    # no matter which sections (summary, breaking, features, ...) were present.
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

        # Heading "# ..." → bold line (drop the marker)
        heading = re.match(r"\s*#{1,6}\s+(.*)", line)
        if heading:
            line = heading.group(1)

        # Bullet "- " / "* " / "+ " → "• " (preserve as list marker)
        bullet = re.match(r"\s*[-*+]\s+(.*)", line)
        if bullet:
            line = bullet.group(1)

        # Escape HTML BEFORE inserting our own tags
        line = _esc(line)

        # Inline code first, so * inside code isn't treated as bold
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        # Bold: **x** or __x__
        line = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"__([^_]+)__", r"<b>\1</b>", line)
        # Links: [text](http...)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', line)

        if heading:
            line = f"<b>{line}</b>"
        if bullet:
            line = f"• {line}"

        lines.append(line)

    html = "\n".join(lines)
    html = re.sub(r"\n{3,}", "\n\n", html)  # collapse runs of blank lines
    return html.strip()


def state_file_for(package_key: str) -> Path:
    # Backward-compatible: the deployed bot already stores OpenClaw in STATE_FILE.
    if package_key == "openclaw":
        return STATE_FILE
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", package_key).strip("-")
    return STATE_DIR / f"last-version-{safe}.txt"


def get_stored_version(package_key: str = "openclaw") -> str:
    try:
        return state_file_for(package_key).read_text().strip()
    except FileNotFoundError:
        return ""


def store_version(package_key: str, version: str):
    path = state_file_for(package_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(version + "\n")


# ── Telegram ────────────────────────────────────────────────────────

def send_telegram(text: str, parse_mode: str = "HTML"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    }).encode()

    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                print(f"[OK] Telegram message sent")
            else:
                print(f"[ERROR] Telegram API: {result}")
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram message: {e}")


# ── Main ────────────────────────────────────────────────────────────

def check_package(package_key: str) -> bool:
    npm_info = get_npm_latest(package_key)
    if not npm_info:
        print(f"[WARN] Could not fetch npm registry for {package_key}")
        return False

    latest = npm_info["version"]
    stored = get_stored_version(package_key)

    if latest == stored:
        return False

    if not stored:
        print(f"[INIT] First run for {package_key}, storing current version: {latest}")
        store_version(package_key, latest)
        return False

    print(f"[NEW] {package_key}: {stored} → {latest}")

    gh_release = get_gh_release(package_key, latest)
    msg = format_release_message(package_key, npm_info, gh_release)
    send_telegram(msg)
    store_version(package_key, latest)
    return True


def check_once() -> bool:
    found = False
    for package_key in WATCH_PACKAGES:
        found = check_package(package_key) or found
    return found


def test_message(package_key: str = "openclaw"):
    npm_info = get_npm_latest(package_key)
    if not npm_info:
        print(f"ERROR: Could not fetch npm registry for {package_key}")
        sys.exit(1)

    gh_release = get_gh_release(package_key, npm_info["version"])
    msg = format_release_message(package_key, npm_info, gh_release)

    # Prepend test banner
    msg = "🧪 <b>TEST — Release Bot Preview</b>\n\n" + msg
    send_telegram(msg)
    print(f"Test message sent for {package_key} {npm_info['version']}")


def daemon():
    print(f"[START] Sovs Release Bot — checking {', '.join(WATCH_PACKAGES)} every {CHECK_INTERVAL}min")

    for package_key in WATCH_PACKAGES:
        npm_info = get_npm_latest(package_key)
        if npm_info:
            stored = get_stored_version(package_key)
            if not stored:
                store_version(package_key, npm_info["version"])
                print(f"[INIT] {package_key}: stored initial version {npm_info['version']}")
            else:
                print(f"[INIT] {package_key}: stored {stored}, latest {npm_info['version']}")

    while True:
        try:
            check_once()
        except Exception as e:
            print(f"[ERROR] Check failed: {e}")
        time.sleep(CHECK_INTERVAL * 60)


def main():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID not set")
        sys.exit(1)

    if "--daemon" in sys.argv:
        daemon()
    elif "--test" in sys.argv:
        package_key = "openclaw"
        if "--package" in sys.argv:
            try:
                package_key = sys.argv[sys.argv.index("--package") + 1]
            except IndexError:
                print("ERROR: --package requires a package key")
                sys.exit(1)
        test_message(package_key)
    else:
        found = check_once()
        if not found:
            versions = ", ".join(f"{p} {get_stored_version(p) or '(checking...)'}" for p in WATCH_PACKAGES)
            print(f"[OK] No new version. Latest: {versions}")


if __name__ == "__main__":
    main()
