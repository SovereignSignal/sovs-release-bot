#!/usr/bin/env python3
"""Tests for release-summary fallback and the invented-number guard.

Run: python3 test_summary.py

Stdlib only. Patches the Ollama HTTP call so nothing leaves the machine.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import bot


PRIMARY = "deepseek-v4.1-flash"
FALLBACK = "glm-5.3-flash"
NOTES = "Fixed the gateway crash in 1.2.3."


def check(name, got, expected):
    if got != expected:
        print(f"FAIL: {name}\n  expected: {expected!r}\n  got:      {got!r}")
        return False
    print(f"ok: {name}")
    return True


def expect(name, cond, detail=""):
    if cond:
        print(f"ok: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _use_models(fallback=FALLBACK):
    bot.OLLAMA_API_KEY = "test-key"
    bot.OLLAMA_MODEL = PRIMARY
    bot.OLLAMA_MODEL_FALLBACK = fallback
    bot.OLLAMA_BASE_URL = "https://ollama.com/api"


def _release(body=NOTES, description=""):
    return (
        "openclaw",
        {"version": "1.2.3", "published": "2026-01-01", "description": description, "package": "openclaw"},
        {"body": body, "html_url": "https://example.com/r", "prerelease": False},
    )


def _capture(script):
    """script items are response dicts or exceptions raised from urlopen."""
    calls = []

    def urlopen(req, timeout=None):
        payload = json.loads(req.data.decode())
        calls.append({
            "url": req.full_url,
            "payload": payload,
            "timeout": timeout,
            "authorization": req.headers.get("Authorization"),
        })
        outcome = script[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(outcome)

    return calls, urlopen


def _run(script, fallback=FALLBACK, body=NOTES, description=""):
    calls, urlopen = _capture(script)
    _use_models(fallback)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        try:
            result = bot.summarize_release(*_release(body, description))
        finally:
            urllib.request.urlopen = original
    return result, calls, buf.getvalue()


def _temps(calls):
    return [call["payload"]["options"]["temperature"] for call in calls]


def _models(calls):
    return [call["payload"]["model"] for call in calls]


def test_defaults():
    env = os.environ.copy()
    env.pop("OLLAMA_MODEL", None)
    env.pop("OLLAMA_MODEL_FALLBACK", None)
    code = (
        "import importlib, bot\n"
        "importlib.reload(bot)\n"
        "assert bot.OLLAMA_MODEL == 'deepseek-v4.1-flash', bot.OLLAMA_MODEL\n"
        "assert bot.OLLAMA_MODEL_FALLBACK == 'glm-5.3-flash', bot.OLLAMA_MODEL_FALLBACK\n"
        "assert bot.OLLAMA_TEMPERATURE <= 0.2, bot.OLLAMA_TEMPERATURE\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
        env=env,
        capture_output=True,
        text=True,
    )
    return expect(
        "code defaults",
        proc.returncode == 0,
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
    )


def test_prompt_has_no_numbers():
    prompt = bot.release_summary_prompt("OpenClaw", ["GitHub release notes:\nFixed the gateway crash."])
    instructions = prompt.split("GitHub release notes:", 1)[0]
    lowered = prompt.lower()
    ok = True
    ok = expect("prompt has no digits in the instructions", not any(ch.isdigit() for ch in instructions)) and ok
    for phrase in ("only facts", "verbatim", "verbs", "hype", "opinion", "speculate", "impact"):
        ok = expect(f"prompt mentions {phrase}", phrase in lowered) and ok
    return ok


def test_guard_tokens():
    results = []
    grounded = bot.ungrounded_summary_numbers
    results.append(check(
        "grounded sentence",
        grounded("Fixes 3 crashes in 1.2.3.", "Fixes 3 crashes in 1.2.3."),
        [],
    ))
    results.append(check(
        "v prefix matches bare version",
        grounded("Ships v1.2.3", "Ships 1.2.3"),
        [],
    ))
    results.append(check(
        "rust-v prefix matches bare version",
        grounded("Ships 0.150.1", "tag rust-v0.150.1"),
        [],
    ))
    results.append(check(
        "longer patch is a different version",
        grounded("Ships 1.2.3", "Ships 1.2.30"),
        ["1.2.3"],
    ))
    results.append(check(
        "longer major is a different version",
        grounded("Ships 1.2.3", "Ships 11.2.3"),
        ["1.2.3"],
    ))
    results.append(check(
        "dropped prerelease is a different version",
        grounded("Ships 2026.7.1", "Ships 2026.7.1-2"),
        ["2026.7.1"],
    ))
    results.append(check(
        "prerelease copied verbatim",
        grounded("Ships 2026.7.1-2.", "Ships 2026.7.1-2."),
        [],
    ))
    results.append(check(
        "integer inside a version is not that integer",
        grounded("3 fixes", "version 1.2.3"),
        ["3"],
    ))
    results.append(check(
        "integer is not a prefix of a longer integer",
        grounded("15 retries", "150 retries"),
        ["15"],
    ))
    results.append(check(
        "leading zero is its own token",
        grounded("build 8", "build 08"),
        ["8"],
    ))
    results.append(check(
        "date parts copied from the notes",
        grounded("Released 2026-08-27", "Released 2026-08-27"),
        [],
    ))
    return all(results)


def test_primary_success_skips_fallback():
    result, calls, log = _run([{"response": "What's new\n- Fixed the gateway crash in 1.2.3."}])
    ok = True
    ok = check("primary summary", result, "What's new\n- Fixed the gateway crash in 1.2.3.") and ok
    ok = check("primary only", _models(calls), [PRIMARY]) and ok
    ok = expect("temperature at most 0.2", _temps(calls) == [0.2] and bot.OLLAMA_TEMPERATURE <= 0.2) and ok
    ok = expect("stream off", calls[0]["payload"]["stream"] is False) and ok
    ok = expect("generate url", calls[0]["url"].endswith("/api/generate")) and ok
    ok = expect("answered by primary", f"answered by {PRIMARY}" in log) and ok
    ok = expect("fallback not logged as the answer", f"answered by {FALLBACK}" not in log) and ok
    return ok


def test_timeout_uses_fallback_once():
    result, calls, log = _run([
        TimeoutError("timed out"),
        {"response": "What's new\n- Fixed the gateway crash in 1.2.3."},
    ])
    ok = True
    ok = check("fallback summary after timeout", result, "What's new\n- Fixed the gateway crash in 1.2.3.") and ok
    ok = check("timeout then fallback", _models(calls), [PRIMARY, FALLBACK]) and ok
    ok = expect("answered by fallback", f"answered by {FALLBACK}" in log) and ok
    ok = expect("primary failure logged", f"Ollama model {PRIMARY} request failed" in log) and ok
    return ok


def test_http_error_uses_fallback_once():
    err = urllib.error.HTTPError("https://ollama.com/api/generate", 500, "boom", hdrs=None, fp=None)
    result, calls, log = _run([
        err,
        {"response": "What's new\n- Fixed the gateway crash in 1.2.3."},
    ])
    ok = True
    ok = check("fallback summary after http error", result, "What's new\n- Fixed the gateway crash in 1.2.3.") and ok
    ok = check("http error then fallback", _models(calls), [PRIMARY, FALLBACK]) and ok
    ok = expect("answered by fallback after http", f"answered by {FALLBACK}" in log) and ok
    return ok


def test_empty_reasoning_reply_uses_fallback():
    result, calls, log = _run([
        {"response": "", "thinking": "The notes mention 9.9.9 which I should not emit"},
        {"response": "What's new\n- Fixed the gateway crash in 1.2.3."},
    ])
    ok = True
    ok = check(
        "reasoning-only primary falls back",
        result,
        "What's new\n- Fixed the gateway crash in 1.2.3.",
    ) and ok
    ok = check("one fallback after empty reply", _models(calls), [PRIMARY, FALLBACK]) and ok
    ok = expect("empty reply logged", f"Ollama model {PRIMARY} returned an empty reply" in log) and ok
    ok = expect("thinking text was not sent", "9.9.9" not in result) and ok
    return ok


def test_missing_response_field_is_empty():
    result, calls, _log = _run([
        {"thinking": "only a reasoning field"},
        {"response": "What's new\n- Fixed the gateway crash in 1.2.3."},
    ])
    ok = check("missing response uses fallback", _models(calls), [PRIMARY, FALLBACK]) and True
    ok = check(
        "missing response result",
        result,
        "What's new\n- Fixed the gateway crash in 1.2.3.",
    ) and ok
    return ok


def test_both_requests_fail_returns_empty():
    result, calls, log = _run([
        TimeoutError("timed out"),
        urllib.error.HTTPError("https://ollama.com/api/generate", 502, "bad gateway", hdrs=None, fp=None),
    ])
    ok = True
    ok = check("both failed", result, "") and ok
    ok = check("both attempted", _models(calls), [PRIMARY, FALLBACK]) and ok
    ok = expect("no model answered", "answered by" not in log) and ok
    ok = expect("drop is for invented numbers", "Dropping release summary" not in log) and ok
    return ok


def test_disabled_fallback_does_not_retry():
    result, calls, _log = _run([TimeoutError("timed out")], fallback="")
    ok = check("no fallback configured", result, "") and True
    ok = check("primary only when fallback empty", _models(calls), [PRIMARY]) and ok
    return ok


def test_same_model_fallback_retries_timeout():
    result, calls, log = _run(
        [
            TimeoutError("timed out"),
            {"response": "What's new\n- Fixed the gateway crash in 1.2.3."},
        ],
        fallback=PRIMARY,
    )
    ok = check("same-model retry", _models(calls), [PRIMARY, PRIMARY]) and True
    ok = check(
        "same-model retry result",
        result,
        "What's new\n- Fixed the gateway crash in 1.2.3.",
    ) and ok
    ok = expect("answered after retry", log.count(f"answered by {PRIMARY}") == 1) and ok
    return ok


def test_guard_retries_fallback_then_keeps_grounded():
    result, calls, log = _run([
        {"response": "What's new\n- Ships 9.9.9 and rewrites the gateway"},
        {"response": "What's new\n- Fixed the gateway crash in 1.2.3."},
    ])
    ok = True
    ok = check("guard keeps fallback", result, "What's new\n- Fixed the gateway crash in 1.2.3.") and ok
    ok = check("guard calls fallback once", _models(calls), [PRIMARY, FALLBACK]) and ok
    ok = expect("primary numbers rejected", f"{PRIMARY} summary has number(s) not in the source: 9.9.9" in log) and ok
    ok = expect("answered by fallback after guard", f"answered by {FALLBACK}" in log) and ok
    ok = expect("invented version not returned", "9.9.9" not in result) and ok
    return ok


def test_guard_drops_when_fallback_also_invents():
    result, calls, log = _run([
        {"response": "Ships 9.9.9"},
        {"response": "Adds 42 endpoints"},
    ])
    ok = True
    ok = check("both ungrounded", result, "") and ok
    ok = check("two attempts then drop", _models(calls), [PRIMARY, FALLBACK]) and ok
    ok = expect("dropping logged", "Dropping release summary" in log) and ok
    ok = expect("neither model answered", "answered by" not in log) and ok
    return ok


def test_guard_drops_when_fallback_request_fails():
    result, calls, log = _run([
        {"response": "Ships 9.9.9"},
        TimeoutError("timed out"),
    ])
    ok = check("ungrounded then timeout", result, "") and True
    ok = check("fallback was the second call", _models(calls), [PRIMARY, FALLBACK]) and ok
    ok = expect("dropped after fallback failure", "Dropping release summary" in log) and ok
    return ok


def test_number_only_past_clip_is_ungrounded():
    body = ("word " * 1200) + "9.9.9"
    result, calls, _log = _run(
        [
            {"response": "Ships 9.9.9"},
            {"response": "What's new\n- Fixed the gateway crash."},
        ],
        body=body + "\nFixed the gateway crash.",
    )
    # The crash sentence is past the 6000-char clip, so the grounded fallback
    # text has to come from a reply with no numbers. "Fixed the gateway crash."
    # has none. 9.9.9 is also past the clip and must not be accepted.
    ok = expect("clipped tail not in prompt", "9.9.9" not in calls[0]["payload"]["prompt"]) and True
    ok = check("number past the clip is rejected", result, "What's new\n- Fixed the gateway crash.") and ok
    ok = check("fallback used for clipped number", _models(calls), [PRIMARY, FALLBACK]) and ok
    return ok


def test_description_number_is_in_the_source():
    result, calls, _log = _run(
        [{"response": "What's new\n- Build 42 fixes the crash."}],
        body="Fixes the crash.",
        description="Build 42",
    )
    ok = check("description number is grounded", result, "What's new\n- Build 42 fixes the crash.") and True
    ok = check("no fallback when description grounds it", _models(calls), [PRIMARY]) and ok
    return ok


def test_reply_text_ignores_reasoning_fields():
    ok = check(
        "whitespace reply is empty",
        bot._model_reply_text({"response": " \n  ", "thinking": "9.9.9"}),
        "",
    )
    ok = check(
        "reasoning field is not the summary",
        bot._model_reply_text({"thinking": "Fixed the gateway crash in 9.9.9"}),
        "",
    ) and ok
    ok = check(
        "non-string response is empty",
        bot._model_reply_text({"response": {"text": "Fixed the gateway crash"}}),
        "",
    ) and ok
    return ok


def test_guard_drops_when_fallback_disabled():
    result, calls, log = _run(
        [{"response": "Ships 9.9.9"}],
        fallback="",
    )
    ok = check("no retry without fallback", result, "") and True
    ok = check("primary was the only call", _models(calls), [PRIMARY]) and ok
    ok = expect("dropped without a second model", "Dropping release summary" in log) and ok
    return ok


def test_alert_without_summary_keeps_plain_notes():
    calls, urlopen = _capture([
        TimeoutError("timed out"),
        urllib.error.HTTPError("https://ollama.com/api/generate", 500, "boom", hdrs=None, fp=None),
    ])
    _use_models()
    original = urllib.request.urlopen
    urllib.request.urlopen = urlopen
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            msg = bot.format_release_message(*_release("Fixed the gateway crash."))
        finally:
            urllib.request.urlopen = original
    ok = True
    ok = expect("plain notes survive both failures", "Fixed the gateway crash." in msg) and ok
    ok = expect("notes section used", "Notes:" in msg) and ok
    ok = check("both models attempted for the alert", _models(calls), [PRIMARY, FALLBACK]) and ok
    return ok


def test_alert_drops_invented_summary():
    calls, urlopen = _capture([
        {"response": "Ships 9.9.9"},
        {"response": "Adds 8.8.8"},
    ])
    _use_models()
    original = urllib.request.urlopen
    urllib.request.urlopen = urlopen
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            msg = bot.format_release_message(*_release("Fixed the gateway crash."))
        finally:
            urllib.request.urlopen = original
    ok = True
    ok = expect("alert still includes the notes", "Fixed the gateway crash." in msg) and ok
    ok = expect("invented 9.9.9 omitted", "9.9.9" not in msg) and ok
    ok = expect("invented 8.8.8 omitted", "8.8.8" not in msg) and ok
    ok = expect("notes section instead of summary", "Notes:" in msg) and ok
    ok = check("guard used both models", _models(calls), [PRIMARY, FALLBACK]) and ok
    return ok


def test_grounded_summary_is_in_the_alert():
    _use_models()
    calls, urlopen = _capture([
        {"response": "What's new\n- Fixed the gateway crash."},
    ])
    original = urllib.request.urlopen
    urllib.request.urlopen = urlopen
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            msg = bot.format_release_message(*_release("Fixed the gateway crash."))
        finally:
            urllib.request.urlopen = original
    ok = True
    ok = expect("summary in alert", "Fixed the gateway crash." in msg) and ok
    ok = expect("plain notes section hidden", "Notes:" not in msg) and ok
    ok = expect("alert log names the model", f"answered by {PRIMARY}" in buf.getvalue()) and ok
    ok = check("single model for a grounded alert", _models(calls), [PRIMARY]) and ok
    return ok


def test_no_key_skips_network():
    bot.OLLAMA_API_KEY = ""
    calls, urlopen = _capture([{"response": "Should not be called"}])
    original = urllib.request.urlopen
    urllib.request.urlopen = urlopen
    try:
        result = bot.summarize_release(*_release())
    finally:
        urllib.request.urlopen = original
    ok = check("missing key", result, "") and True
    ok = check("missing key makes no request", calls, []) and ok
    return ok


def main():
    # Reload is isolated in the defaults subprocess. This process mutates bot
    # globals on purpose and restores the key at the end.
    saved_key = bot.OLLAMA_API_KEY
    saved_model = bot.OLLAMA_MODEL
    saved_fallback = bot.OLLAMA_MODEL_FALLBACK
    tests = [
        test_defaults,
        test_prompt_has_no_numbers,
        test_guard_tokens,
        test_primary_success_skips_fallback,
        test_timeout_uses_fallback_once,
        test_http_error_uses_fallback_once,
        test_empty_reasoning_reply_uses_fallback,
        test_missing_response_field_is_empty,
        test_both_requests_fail_returns_empty,
        test_disabled_fallback_does_not_retry,
        test_same_model_fallback_retries_timeout,
        test_guard_retries_fallback_then_keeps_grounded,
        test_guard_drops_when_fallback_also_invents,
        test_guard_drops_when_fallback_request_fails,
        test_guard_drops_when_fallback_disabled,
        test_reply_text_ignores_reasoning_fields,
        test_number_only_past_clip_is_ungrounded,
        test_description_number_is_in_the_source,
        test_alert_without_summary_keeps_plain_notes,
        test_alert_drops_invented_summary,
        test_grounded_summary_is_in_the_alert,
        test_no_key_skips_network,
    ]
    try:
        results = [fn() for fn in tests]
    finally:
        bot.OLLAMA_API_KEY = saved_key
        bot.OLLAMA_MODEL = saved_model
        bot.OLLAMA_MODEL_FALLBACK = saved_fallback
    if all(results):
        print("\nAll tests passed.")
        return 0
    print("\nSome tests FAILED.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
