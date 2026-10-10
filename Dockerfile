# OpenClaw Release Bot — stdlib-only Python daemon.
FROM python:3.12-slim

WORKDIR /app

# No third-party deps; bot.py uses only the standard library.
COPY bot.py ai_wire.py release_events.py runner.py ./

# Unbuffered stdout so Railway log streaming is live.
ENV PYTHONUNBUFFERED=1

# Persist version baselines on a Railway volume mounted at /data
# (add the volume in the Railway dashboard; these env defaults point state there).
ENV STATE_DIR=/data \
    STATE_FILE=/data/last-version.txt

CMD ["python3", "runner.py"]
