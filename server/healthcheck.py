#!/usr/bin/env python3
"""
The Worbus Server image's HEALTHCHECK (Worbus P4, design §6.1). Standard library only.

Exit 0 when both halves answer with a 2xx, within 2 s each:
  - the hub's plain-HTTP health port (HUB_HEALTH_PORT, else HUB_PORT + 1, else 4791,
    the way hub.py resolves it);
  - lb-bot's open `/api/health` (LB_BOT_WEB_PORT, else 8899).
Exit 1 otherwise, with one line naming the half that did not answer (Docker keeps it
in `docker inspect`'s health log). The image retries four times 30 s apart before it
calls the container unhealthy, so an lb-bot the launcher is restarting is not
reported for about two minutes.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 2.0


def _port(env, name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    return int(raw) if raw.isdigit() else default


def targets(env=os.environ) -> list[tuple[str, str]]:
    hub_port = _port(env, "HUB_PORT", 4790)
    health = (env.get("HUB_HEALTH_PORT") or "").strip()
    hub_health = int(health) if health.isdigit() else hub_port + 1
    lb_port = _port(env, "LB_BOT_WEB_PORT", 8899)
    return [("hub", f"http://127.0.0.1:{hub_health}/"),
            ("lb-bot", f"http://127.0.0.1:{lb_port}/api/health")]


def answers(url: str) -> str | None:
    """None when `url` answers 2xx, else why not (no body, no headers printed)."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            r.read(4096)
            return None if 200 <= r.status < 300 else f"answered {r.status}"
    except urllib.error.HTTPError as e:
        return f"answered {e.code}"
    except Exception as e:  # noqa: BLE001
        return f"no answer ({type(e).__name__})"


def main() -> int:
    bad = []
    for name, url in targets():
        why = answers(url)
        if why:
            bad.append(f"{name} {why} at {url}")
    if bad:
        print("unhealthy: " + "; ".join(bad))
        return 1
    print("healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
