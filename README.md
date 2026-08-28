# Sovs Release Bot

Telegram bot that alerts when new **OpenClaw**, **Hermes Agent**, **Codex**, and **Claude Code** versions are published. It watches both the npm `latest` tag **and** GitHub releases, because those channels often diverge (OpenClaw betas, Hermes date tags, Codex `rust-v*` tags).

## What it does

- Monitors configured npm `latest` tags **and** GitHub releases every 30 minutes (configurable)
- OpenClaw prereleases are on by default (their `latest` npm tag has been stale while betas ship); Codex/Hermes/Claude Code alert on stables unless you set `WATCH_PRERELEASES`
- When a new version is detected → sends a Telegram alert with:
  - Package name
  - Version number
  - Optional LLM-generated "What's new" summary when configured
  - Publish timestamp
  - Docker image tag when available
  - GitHub release notes (if available)
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
| `OLLAMA_API_KEY` | No | — | Enables optional release summaries through the Ollama generate API. Leave unset to use normal changelog formatting only. |
| `OLLAMA_BASE_URL` | No | `https://ollama.com/api` | Ollama API base URL. |
| `OLLAMA_MODEL` | No | `gpt-oss:20b` | Model name sent to Ollama. |
| `SUMMARY_TIMEOUT` | No | `30` | Seconds to wait for the optional summary before falling back. |

The summary path is optional and fail-closed: if `OLLAMA_API_KEY` is missing, the API times out, or the model returns an empty response, alerts use the existing parsed changelog and metadata without crashing.

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
python3 test_format.py   # Markdown→Telegram-HTML converter
python3 test_watch.py    # version matching, GitHub tag prefixes, prerelease flags
```

Stdlib-only asserts, no test framework. `test_format.py` covers
`md_to_telegram_html()`. `test_watch.py` covers the detection helpers that
decide when OpenClaw / Hermes / Codex / Claude Code should alert.

## License

MIT
