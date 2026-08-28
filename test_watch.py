#!/usr/bin/env python3
"""Tests for release detection helpers. Run: python3 test_watch.py"""

import os

import bot


def check(name, got, expected):
    if got != expected:
        print(f"FAIL: {name}\n  expected: {expected!r}\n  got:      {got!r}")
        return False
    print(f"ok: {name}")
    return True


def main():
    results = []

    results.append(check("norm rust-v", bot.normalize_version("rust-v0.150.1"), "0.150.1"))
    results.append(check("norm v", bot.normalize_version("v2.1.250"), "2.1.250"))
    results.append(check("norm date", bot.normalize_version("v2026.8.27"), "2026.8.27"))
    results.append(check("norm plain", bot.normalize_version("0.20.6"), "0.20.6"))

    results.append(check(
        "token match rust",
        bot.version_in_text("0.150.1", "rust-v0.150.1"),
        True,
    ))
    results.append(check(
        "token no prefix false positive",
        bot.version_in_text("0.150.1", "rust-v0.150.10"),
        False,
    ))
    results.append(check(
        "token in hermes title",
        bot.version_in_text("0.20.6", "Hermes Agent v0.20.6 (v2026.8.27)"),
        True,
    ))
    results.append(check(
        "token openclaw mismatch",
        bot.version_in_text("2026.7.1-2", "v2026.8.1-beta.3"),
        False,
    ))

    results.append(check(
        "codex tags",
        bot.github_tag_candidates("codex", "0.150.1"),
        ["rust-v0.150.1", "v0.150.1", "0.150.1"],
    ))
    results.append(check(
        "claude tags",
        bot.github_tag_candidates("claude-code", "2.1.250"),
        ["v2.1.250", "2.1.250"],
    ))

    hermes = {
        "tag_name": "v2026.8.27",
        "name": "Hermes Agent v0.20.6 (v2026.8.27)",
    }
    results.append(check(
        "hermes date tag matches npm semver",
        bot.release_matches_version(hermes, "0.20.6"),
        True,
    ))
    results.append(check(
        "hermes date tag does not match older npm",
        bot.release_matches_version(hermes, "0.20.5"),
        False,
    ))

    info = bot.npm_info_from_github(
        "hermes-agent",
        {
            "tag_name": "v2026.8.27",
            "name": "Hermes Agent v0.20.6 (v2026.8.27)",
            "published_at": "2026-08-27T12:06:53Z",
        },
        {"description": "bridge", "package": "hermes-agent"},
    )
    results.append(check("hermes display version", info["version"], "0.20.6"))

    results.append(check("openclaw prereleases default", bot.watches_prereleases("openclaw"), True))
    results.append(check("codex prereleases default", bot.watches_prereleases("codex"), False))

    os.environ["WATCH_PRERELEASES"] = "none"
    results.append(check("prereleases none", bot.watches_prereleases("openclaw"), False))
    os.environ["WATCH_PRERELEASES"] = "all"
    results.append(check("prereleases all", bot.watches_prereleases("codex"), True))
    os.environ["WATCH_PRERELEASES"] = "codex"
    results.append(check("prereleases csv hit", bot.watches_prereleases("codex"), True))
    results.append(check("prereleases csv miss", bot.watches_prereleases("openclaw"), False))
    del os.environ["WATCH_PRERELEASES"]

    results.append(check(
        "hermes github repo",
        bot.package_config("hermes-agent")["github"],
        "NousResearch/hermes-agent",
    ))

    changelog = bot.parse_changelog("## What's changed\n\n* fix(foo): bar by @x in https://github.com/a/b/pull/9\n")
    results.append(check("changelog case-insensitive header", changelog["fixes"], ["bar (#9)"]))

    # Free-form notes (Hermes) must not crash when there is no What's Changed section
    hermes_notes = bot.parse_changelog("# Hermes Agent v0.20.6\n\nPatch release. This tag rolls up PRs.\n")
    results.append(check("hermes notes no crash", hermes_notes["fixes"], []))
    results.append(check("hermes notes excerpt", hermes_notes["excerpt"][0].startswith("Patch release"), True))

    excerpt = bot.extract_plain_excerpt(
        "# Title\n\n---\n\nPatch release. Ships the last week of work.\n\n- Consent-gated browsing\n\nFull Changelog: nope"
    )
    results.append(check("excerpt skips headings", excerpt[0], "Patch release. Ships the last week of work."))
    results.append(check("excerpt keeps bullets", excerpt[1], "Consent-gated browsing"))

    synced = bot._sync_matching_channel(
        {"npm": "0.20.6", "github_tag": ""},
        "0.20.6",
        hermes,
    )
    results.append(check("sync same hermes release", synced, {"npm": "0.20.6", "github_tag": "v2026.8.27"}))
    results.append(check(
        "no sync different release",
        bot._sync_matching_channel(
            {"npm": "2026.7.1-2", "github_tag": ""},
            "2026.7.1-2",
            {"tag_name": "v2026.8.1-beta.3", "name": "OpenClaw 2026.8.1-beta.3"},
        ),
        None,
    ))

    if all(results):
        print("\nAll tests passed.")
        return 0
    print("\nSome tests FAILED.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
