#!/usr/bin/env python3
"""Tests for message formatting. Run: python3 test_format.py

No third-party deps — uses assert and exits non-zero on failure so it works
as a lightweight CI/pre-deploy check for the stdlib-only bot.
"""

import bot


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

    if all(results):
        print("\nAll tests passed.")
        return 0
    print("\nSome tests FAILED.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
