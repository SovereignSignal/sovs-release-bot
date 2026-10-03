#!/usr/bin/env python3
"""Run legacy polling and the Release Events inbox together."""
import os
import threading
import traceback

import bot
import release_events


def _run_inbox():
    """Serve HTTP. If this thread dies, exit non-zero.

    Chosen policy: exit so Railway ON_FAILURE restarts the process, poller
    included. A dead inbox must not leave the poller looking healthy.
    Request errors stay in the handler and do not stop ``bot.daemon()``.
    """
    try:
        release_events.serve()
    except Exception:
        traceback.print_exc()
        print("[EVENTS][ERROR] http inbox thread died", flush=True)
        os._exit(1)
    print("[EVENTS][ERROR] http inbox thread died", flush=True)
    os._exit(1)


def main():
    release_events.wire(bot)
    inbox = threading.Thread(target=_run_inbox, name="release-events", daemon=True)
    inbox.start()
    bot.daemon()


if __name__ == "__main__":
    main()
