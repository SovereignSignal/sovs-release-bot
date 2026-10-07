# Sovs Release Bot

Telegram bot that alerts when new **OpenClaw**, **Hermes Agent**, **Codex**, and **Claude Code** versions are published. It watches both the npm `latest` tag **and** GitHub releases, because those channels often diverge (OpenClaw betas, Hermes date tags, Codex `rust-v*` tags).

## What it does

- Monitors configured npm `latest` tags **and** GitHub releases every 30 minutes (configurable)
- OpenClaw prereleases are on by default (their `latest` npm tag has been stale while betas ship); Codex/Hermes/Claude Code alert on stables unless you set `WATCH_PRERELEASES`
- When a new version is detected → sends a Telegram alert with:
  - Package name and version
  - A short plain summary (a few bullets). An LLM summary is used when configured and every number in it appears in the source; otherwise the first few cleaned release-note bullets are used
  - Publish time in Eastern Time when a timestamp is available
  - Docker image tag when the package has one
  - A Release notes link
- Zero dependencies (stdlib only)

## Deploy on Railway

The bot runs well on **Railway** as a worker (no web port), built from the
`Dockerfile`. To set it up:

1. **New service** → Deploy from this GitHub repo (or `railway up` from a clone).
   Railway uses the `Dockerfile` automatically.
2. **Add a volume mounted at `/data`** so version baselines survive redeploys.
   Without it the bot just re-baselines on restart — no spam, but it can miss a
   release published during a redeploy. The Dockerfile already defaults
   `STATE_DIR=/data` and `STATE_FILE=/data/last-version.txt`.
3. **Set service variables** (see the table below) — at minimum `TELEGRAM_BOT_TOKEN`
   and `TELEGRAM_CHAT_ID`; add `OLLAMA_API_KEY` to enable summaries.
4. Deploy. Start command: `python3 bot.py --daemon`.

## Self-host (local / systemd)

Prefer a container platform like Railway above for the simplest setup. The
systemd path below is provided for self-hosting on your own server or local runs.

```bash
# Clone
git clone https://github.com/SovereignSignal/sovs-release-bot
cd sovs-release-bot

# Run setup (creates systemd service)
chmod +x setup.sh
sudo ./setup.sh

# Configure
sudo nano /opt/sovs-release-bot/.env

# Start
sudo systemctl start sovs-release-bot

# Test
TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python3 bot.py --test
TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python3 bot.py --test --package hermes-agent
python3 bot.py --status
```

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | — | Chat ID for alerts |
| `CHECK_INTERVAL` | No | 30 | Minutes between checks |
| `STATE_FILE` | No | `/opt/sovs-release-bot/data/last-version.txt` | Backward-compatible OpenClaw version state file |
| `STATE_DIR` | No | `/opt/sovs-release-bot/data` | State directory for non-OpenClaw packages |
| `WATCH_PACKAGES` | No | `openclaw,hermes-agent,codex,claude-code` | Comma-separated package keys or npm package names |
| `WATCH_PRERELEASES` | No | package defaults (OpenClaw on) | `all`, `none`, or CSV of package keys. Overrides which GitHub prereleases are treated as new versions. |
| `OLLAMA_API_KEY` | No | — | Enables optional release summaries through the Ollama generate API. Leave unset to send the cleaned release-note bullets only. |
| `OLLAMA_BASE_URL` | No | `https://ollama.com/api` | Ollama API base URL. |
| `OLLAMA_MODEL` | No | `deepseek-v4.1-flash` | Primary model name sent to Ollama. A value already set in the service environment overrides this default. |
| `OLLAMA_MODEL_FALLBACK` | No | `glm-5.3-flash` | Tried once when the primary times out, returns an HTTP error, returns an empty reply, or includes a number that is not in the source. Set to empty to skip that retry. |
| `SUMMARY_TIMEOUT` | No | `30` | Seconds to wait for each summary request before trying the fallback or sending the alert without a summary. |

The summary path is optional and fail-closed. The prompt asks for facts from the release notes only: versions and numbers copied verbatim, the source's verbs, no hype or opinion, and no speculation about impact. Sampling temperature is `0.2`. A reply that only fills reasoning fields counts as empty. If the summary contains a number or version that is not in the npm description or GitHub release notes, the fallback model is tried once; if that attempt also fails the check, the alert goes out without the model summary. If `OLLAMA_API_KEY` is missing, or both models fail, the alert still sends a few cleaned bullets from the release notes (Markdown removed, cut on a sentence or bullet) instead of a raw changelog. The npm package description is not repeated on every alert. The log line `Release summary answered by <model>` names which model produced the summary. Forwarded inbox events use this same summary and the same alert layout.

## Deployment notes

Run as a worker (no web port), built from the `Dockerfile`. Mount a persistent
volume at `/data` so the version baselines in `/data/last-version.txt` (and
per-package files) survive redeploys. Required service variables:
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, plus optional `OLLAMA_API_KEY` to
enable "What's new" summaries.

The repo also ships a systemd setup (`setup.sh`, `deploy/systemd/`) for
self-hosting on your own server.

- Create the bot and get its token from [@BotFather](https://t.me/BotFather).
- Alerts are delivered to the chat set by `TELEGRAM_CHAT_ID`. Use a direct
  message chat ID for a private feed, or a channel/group ID to broadcast.

## Running modes

```bash
python3 bot.py              # Check once, exit
python3 bot.py --daemon     # Run continuously
python3 bot.py --status     # Print npm + GitHub vs stored baselines (no Telegram)
python3 bot.py --test       # Send OpenClaw test message
python3 bot.py --test --package hermes-agent
python3 bot.py --test --package all
```

## Tests

```bash
python3 test_format.py    # Markdown conversion and the shared alert layout
python3 test_watch.py     # version matching, GitHub tag prefixes, prerelease flags
python3 test_summary.py   # summary fallback and invented-number guard
```

Stdlib-only asserts, no test framework. `test_format.py` covers
`md_to_telegram_html()`. `test_watch.py` covers the detection helpers that
decide when OpenClaw / Hermes / Codex / Claude Code should alert.
`test_summary.py` covers the Ollama fallback and the number guard.

## License

MIT
