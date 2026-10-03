#!/usr/bin/env python3
"""Run legacy polling and Release Events inbox together."""
import threading
import bot
import release_events

t=threading.Thread(target=release_events.serve,name="release-events",daemon=True)
t.start()
bot.daemon()
