"""An append-only record of who changed what, and who tried to get in.

The other stores answer "who may do this"; this one answers, after the fact,
"who did". One line per event in `output/audit.jsonl`: every non-GET API
call to a protected endpoint (recorded by middleware, so a new router is
covered the day it exists), every login success and failure, and the one GET
that is really a disclosure -- revealing a stored LUKS passphrase.

JSON Lines for the same reasons as deployments.jsonl: append-only in normal
use, survives a torn final line, and greppable when the web UI is not the
tool you want. Bounded the same way too -- trimmed oldest-first past ~20k
events, because an audit log that can fill the disk becomes its own
denial of service.

Recording never raises. Losing one audit line must not fail the request it
describes -- an audit log that can veto the work being audited is a bigger
availability problem than a gap in it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from app import forwarder as _forwarder
from app.config import settings

MAX_EVENTS = 20000
TRIM_TO = 15000

_FILE = "audit.jsonl"
_lock = threading.Lock()


def _path() -> str:
    return os.path.join(settings.output_dir, _FILE)


def record(actor: str, role: str, method: str, path: str, status: int,
           ip: str = "", summary: str = "", **extra: Any) -> None:
    """Append one event. Never raises; never logs a secret -- callers pass
    usernames and summaries, not passwords or tokens."""
    row = {"ts": time.time(), "actor": actor, "role": role, "method": method,
           "path": path, "status": status, "ip": ip}
    if summary:
        row["summary"] = summary
    row.update({k: v for k, v in extra.items() if v not in (None, "")})
    # Off the box first, and never blocking on it -- see forwarder.py. The local
    # file is bounded and trimmed oldest-first, so it is a buffer rather than an
    # archive; a collector that has the event is the one that still has it in a
    # year, or after somebody with root decides it should not exist.
    #
    # Before the local write on purpose: if the disk is full, the line that
    # cannot be written here is exactly the one worth having somewhere else.
    try:
        _forwarder.forwarder.submit(dict(row))
    except Exception:                                    # noqa: BLE001
        pass
    try:
        with _lock:
            os.makedirs(settings.output_dir, exist_ok=True)
            with open(_path(), "a") as f:
                f.write(json.dumps(row) + "\n")
            _trim_if_huge()
    except OSError:
        pass


def _trim_if_huge() -> None:
    try:
        if os.path.getsize(_path()) < MAX_EVENTS * 150:
            return
        with open(_path()) as f:
            lines = f.readlines()
        if len(lines) <= MAX_EVENTS:
            return
        tmp = _path() + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(lines[-TRIM_TO:])
        os.replace(tmp, _path())
    except OSError:
        pass


def events(since: float | None = None, limit: int = 500,
           actor: str = "") -> list[dict]:
    """Newest first, optionally only one actor or only after a timestamp."""
    try:
        with open(_path()) as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue          # a torn final line is expected, not a fault
        if since is not None and row.get("ts", 0) <= since:
            continue
        if actor and row.get("actor") != actor:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out
