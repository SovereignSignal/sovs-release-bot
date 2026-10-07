#!/usr/bin/env python3
"""Tests for message formatting. Run: python3 test_format.py

No third-party deps — uses assert and exits non-zero on failure so it works
as a lightweight CI/pre-deploy check for the stdlib-only bot.
"""

import bot
import release_events as inbox


def check(name, got, expected):
    if got != expected:
        print(f"FAIL: {name}\n  expected: {expected!r}\n  got:      {got!r}")
        return False
    print(f"ok: {name}")
    return True


def main():
    md = bot.md_to_telegram_html
    results = []

    # Bold markers become <b>, not literal asterisks
    results.append(check("bold", md("**Durable delivery**"), "<b>Durable delivery</b>"))

    # Inline code becomes <code>
    results.append(check("code", md("use `sendMessage` now"), "use <code>sendMessage</code> now"))

    # Bullet markers normalize to •
    results.append(check("bullet", md("- first item"), "• first item"))
    results.append(check("bullet-star", md("* second item"), "• second item"))

    # Markdown heading becomes a bold line (no leading #)
    results.append(check("heading", md("## What's new"), "<b>What's new</b>"))

    # Bold heading line stays bold (model's actual output)
    results.append(check("bold-heading", md("**What's new**"), "<b>What's new</b>"))

    # HTML special chars are escaped so they can't break parse_mode=HTML
    results.append(check("escape", md("a < b & c > d"), "a &lt; b &amp; c &gt; d"))

    # Code containing angle brackets escapes the content but keeps the tag
    results.append(check("code-escape", md("`List<T>`"), "<code>List&lt;T&gt;</code>"))

    # snake_case identifiers must NOT be mangled into italics
    results.append(check("snake_case", md("the secret_ref_value stays"), "the secret_ref_value stays"))

    # Links convert to anchors
    results.append(check(
        "link",
        md("see [the docs](https://example.com/x)"),
        'see <a href="https://example.com/x">the docs</a>',
    ))

    # A realistic multi-line summary
    realistic = "**What's new**\n- **Durable delivery** via `sendMessage`\n- Fewer stale replies"
    expected = "<b>What's new</b>\n• <b>Durable delivery</b> via <code>sendMessage</code>\n• Fewer stale replies"
    results.append(check("multiline", md(realistic), expected))

    # Excess blank lines collapse and edges trim
    results.append(check("trim", md("\n\nhi\n\n\n\nbye\n\n"), "hi\n\nbye"))

    saved_key = bot.OLLAMA_API_KEY
    bot.OLLAMA_API_KEY = ""
    try:
        results.extend(_alert_layout_checks())
    finally:
        bot.OLLAMA_API_KEY = saved_key

    if all(results):
        print("\nAll tests passed.")
        return 0
    print("\nSome tests FAILED.")
    return 1


CLINE_TRUNCATED = """### Changed

- If a model's response ends without a recognized finish reason, Cline asks it to continue once instead of treating the response as complete.
- Refreshed the model catalog. The Cline recommended list adds GPT-6.1 Sol, and the free list adds Solar Mini 4 and drops DeepSeek V4.1 Flash and space-bunny-alpha. Default models change for 302.AI, AKI.IO, Blue Claw, CoralBricks, CrossModel, DevPass, DigitalOcean, GMI Cloud, LLM Gateway, Mistral, NanoGPT, Neon, Nvidia, Ofox, Requesty, Subcon"""

# First 500 characters of the Kilo Code 7.8.8 notes, the clip ClawBytes forwards.
KILO_CLIPPED = """## VS Code

### 7.8.8

#### Minor Changes

- [#14891](https://github.com/Kilo-Org/kilocode/pull/14891) [`ce9a04b`](https://github.com/Kilo-Org/kilocode/commit/ce9a04b0f91531b26d37a0f514621396f85311e8) - Pin worktrees in the Agent Manager sidebar with Shift+click or the context menu. Pinned worktrees stay at the top of the list, get the first jump shortcuts, and keep their section for when you unpin them.

- [#14885](https://github.com/Kilo-Org/kilocode/pull/14885) [`0abb254`](https://github.com/"""

OPEN_INTERPRETER = """# Open Interpreter 0.0.56

## Models

- Add GPT-6.1 Sol (`gpt-6.1-sol`) metadata and refresh the maintained provider
  catalog and model documentation.
- Add the current Anthropic `claude-sonnet-5-5` choice while retaining
  provider-specific availability rules and the existing catalog safeguards.

## Fixes

- Rebind existing shell-environment values when resuming an eligible idle
  thread, with validation that preserves the loaded thread's shell policy and
  does not expose environment values.
"""

MARKDOWN_HEAVY = """# Widget 2.0

### Changed

- **Generate Commit Message** now follows your `.clinerules`.
- See [the guide](https://example.com/guide) for the steps.

```
secret fence
```

**Full Changelog**: https://example.com/compare

### Fixed

- Claude no longer fails with a 400 error when the base URL is custom.
"""


def _event(name, version, summary, url):
    return {
        "schema": "release-event/v1",
        "id": "software:github:example:v1",
        "kind": "software",
        "name": name,
        "version": version,
        "source": "clawbytes",
        "url": url,
        "summary": summary,
    }


def _bullets(text):
    return [line for line in text.splitlines() if line.startswith("•")]


def _alert_layout_checks():
    results = []
    cline_url = "https://github.com/cline/cline/releases/tag/v4.1.23"
    cline = inbox.format_event(_event("Cline", "4.1.23", CLINE_TRUNCATED, cline_url))
    results.append(check("cline has no raw heading", "###" in cline, False))
    results.append(check("cline has no bold markers", "**" in cline, False))
    results.append(check("cline drops the cut word", "Subcon" in cline, False))
    results.append(check("cline drops the cut sentence", "Default models change" in cline, False))
    results.append(check(
        "cline keeps the finish-reason bullet",
        "recognized finish reason" in cline,
        True,
    ))
    results.append(check("cline keeps the catalog sentence", "space-bunny-alpha" in cline, True))
    results.append(check("cline marks the trimmed tail", "…" in cline, True))
    results.append(check("cline link label", "Release notes</a>" in cline, True))
    results.append(check("cline link target", f'href="{cline_url}"' in cline, True))
    results.append(check("cline header", "<b>Cline 4.1.23</b>" in cline, True))
    results.append(check("cline stays valid html", cline.count("<"), 4))

    builtin = bot.format_release_message(
        "cline",
        {
            "version": "4.1.23",
            "published": "2026-10-07T11:02:00Z",
            "description": "The same tagline every time",
            "package": "cline",
        },
        {"body": CLINE_TRUNCATED, "html_url": cline_url, "prerelease": False},
    )
    results.append(check(
        "builtin uses the same bullets",
        _bullets(builtin),
        _bullets(cline),
    ))
    results.append(check("builtin hides the repeated description", "tagline" in builtin, False))
    results.append(check("builtin hides changelog sections", "Features:" in builtin or "Fixes:" in builtin or "Notes:" in builtin, False))
    results.append(check("builtin formats the timestamp", "Oct 7, 7:02 AM ET" in builtin, True))
    results.append(check("builtin hides the raw timestamp", "2026-10-07T11:02:00Z" in builtin, False))
    results.append(check("builtin link label", "Release notes</a>" in builtin, True))

    description_only = bot.format_release_message(
        "cline",
        {
            "version": "4.1.23",
            "published": "unknown",
            "description": "Useful only when notes are missing.",
            "package": "cline",
        },
        None,
    )
    results.append(check(
        "description is the summary when notes are missing",
        "Useful only when notes are missing." in description_only,
        True,
    ))
    results.append(check("unknown published time is omitted", "unknown" in description_only.lower(), False))

    kilo = bot.plain_alert_text(KILO_CLIPPED, title="Kilo Code 7.8.8", version="7.8.8")
    results.append(check("kilo drops pr links", "[#" in kilo or "14891" in kilo, False))
    results.append(check("kilo drops commit links", "ce9a04b" in kilo or "0abb254" in kilo, False))
    results.append(check("kilo drops link syntax", "](" in kilo, False))
    results.append(check("kilo keeps the finished bullet", "Pin worktrees in the Agent Manager sidebar" in kilo, True))
    results.append(check("kilo does not keep the clipped url", "https://github.com/" in kilo, False))
    results.append(check("kilo ellipsis", kilo.endswith("…"), True))

    interpreter = inbox.format_event(_event(
        "Open Interpreter",
        "0.0.56",
        OPEN_INTERPRETER,
        "https://github.com/openinterpreter/open-interpreter/releases/tag/rust-v0.0.56",
    ))
    results.append(check("interpreter title is not repeated", interpreter.count("Open Interpreter 0.0.56"), 1))
    results.append(check("interpreter joins wrapped bullets", "catalog and model documentation" in interpreter, True))
    results.append(check("interpreter strips code ticks", "`" in interpreter, False))
    results.append(check("interpreter keeps the model id", "gpt-6.1-sol" in interpreter, True))
    plain_title = bot.plain_alert_text(
        "Open Interpreter 0.0.56\n\n- Add a model.",
        title="Open Interpreter 0.0.56",
        version="0.0.56",
    )
    results.append(check("plain duplicate title is dropped", plain_title, "• Add a model."))

    heavy = bot.plain_alert_text(MARKDOWN_HEAVY, title="Widget 2.0", version="2.0")
    results.append(check("heavy has no headings", "###" in heavy or "# Widget" in heavy, False))
    results.append(check("heavy has no bold markers", "**" in heavy, False))
    results.append(check("heavy has no code ticks", "`" in heavy, False))
    results.append(check("heavy has no raw links", "](" in heavy or "https://example.com/guide" in heavy, False))
    results.append(check("heavy keeps bold text", "Generate Commit Message" in heavy, True))
    results.append(check("heavy keeps code text", ".clinerules" in heavy, True))
    results.append(check("heavy keeps link labels", "the guide" in heavy, True))
    results.append(check("heavy drops fenced code", "secret fence" in heavy, False))
    results.append(check("heavy drops the changelog line", "Full Changelog" in heavy, False))

    tool = bot.plain_alert_text(
        "- Reworked the runner so a partial tool call can resume without dropping the tool-r"
    )
    results.append(check("hyphen cut drops the partial word", "tool-r" in tool, False))
    results.append(check("hyphen cut keeps the sentence", "partial tool call" in tool, True))
    results.append(check("hyphen cut adds an ellipsis", tool.endswith("…"), True))

    long_cut = ("Updated the catalog for many providers " * 8).strip() + " Subcon"
    clipped = bot.plain_alert_text(long_cut)
    results.append(check("long cut drops the partial token", "Subcon" in clipped, False))
    results.append(check("long cut adds an ellipsis", clipped.endswith("…"), True))
    results.append(check(
        "short fragment stays whole",
        bot.plain_alert_text("- Consent-gated browsing"),
        "• Consent-gated browsing",
    ))
    results.append(check(
        "snake_case in a bullet stays",
        bot.plain_alert_text("the secret_ref_value stays"),
        "• the secret_ref_value stays",
    ))

    changelog = """## What's Changed

* feat(ui): Add a button by @ada in https://github.com/a/b/pull/9
* fix(api): Stop the crash by @bea in https://github.com/a/b/pull/10
"""
    polled = bot.format_release_message(
        "codex",
        {"version": "0.1.0", "published": "2026-10-07T15:30:00Z", "description": "Codex CLI", "package": "@openai/codex"},
        {"body": changelog, "html_url": "https://github.com/openai/codex/releases/tag/rust-v0.1.0", "prerelease": False},
    )
    results.append(check("polled alert has no feature section", "Features:" in polled or "Fixes:" in polled, False))
    results.append(check("polled alert cleans commit subjects", "Add a button" in polled and "Stop the crash" in polled, True))
    results.append(check("polled alert drops pr urls", "pull/9" in polled, False))
    results.append(check("polled alert hides the package blurb", "Codex CLI" in polled, False))
    results.append(check("afternoon timestamp is ET", "Oct 7, 11:30 AM ET" in polled, True))

    fitted = bot.fit_text_to_message(
        "• Alpha beta gamma.\n• Delta epsilon zeta.",
        lambda text: text,
        limit=28,
    )
    results.append(check("fit stays inside the limit", len(fitted) <= 28, True))
    results.append(check("fit keeps the finished sentence", "gamma." in fitted, True))
    results.append(check("fit does not start the next word", "Delta" in fitted or "eps" in fitted, False))

    results.append(check(
        "published example",
        bot.format_published("2026-10-07T11:02:00Z"),
        "Oct 7, 7:02 AM ET",
    ))
    return results


if __name__ == "__main__":
    raise SystemExit(main())
