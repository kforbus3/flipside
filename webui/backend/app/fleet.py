"""Live fleet state: who is out there, what they are running, and when they last said so.

This is the half of the fleet picture that `deployments.py` deliberately cannot
give. That file is an append-only log of two provisioning events -- "imaged" and
"booted" -- and it is exactly right for the question it answers: what did this
server hand out, and did the machine come back. It is the wrong shape for the
question an operator asks on any other day, which is "what is out there *now*",
because a log of things that happened once cannot tell you that a machine which
booted fine in March stopped answering in June. Under the old boot-only check-in
a machine that died three weeks after imaging read `running` forever.

So machines run an agent (`ab-agent.sh`) that reports in on a timer, and this
module holds what it says. Two stores, split by who owns the data:

  hosts.json    Operator-owned and durable: group membership, a label, whether
                the host is held back from rollouts. Written rarely, by people.

  state.json    Machine-owned: last seen, running version, slot, address, what
                the agent is doing about any update it was offered. Written
                constantly, by machines, and cheap to lose.

The second is kept in memory and flushed on a debounce rather than written
through on every heartbeat. A thousand machines on a five-minute timer is a
heartbeat every 300ms, and rewriting the whole file that often would be the
busiest thing this server does -- to protect data whose worst-case loss is a
`last_seen` that is thirty seconds stale on a process that just crashed. The
next heartbeat repairs it. Durability that costs more than what it protects is
worth is not a feature.

The reachability assumption is stated once, here, because everything else
depends on it: machines reach the server, never the other way round. A machine
is imaged on a private provisioning switch and then moves to wherever it lives,
which is behind NAT, a firewall, or simply an address this server never learns.
Nothing here ever opens a connection to a machine, which is why updates are
offered in a heartbeat response and pulled, rather than pushed.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from .config import settings

# How long a machine may go unheard-from before its row stops meaning "this is
# true now". Expressed in multiples of the agent's own interval rather than as
# flat minutes so that changing the interval does not silently make every
# machine look broken (or hide one that is).
MISSED_BEATS_ONLINE = 3      # up to 3 missed heartbeats: still "online"
STALE_SECONDS = 86400        # beyond that, but seen inside a day: "stale"
                             # past a day with no word at all: "offline"

# Debounce for the state flush. Long enough that a busy fleet writes the file
# every few seconds rather than every heartbeat; short enough that a restart
# loses almost nothing.
FLUSH_AFTER_SECONDS = 5.0

# A cap, so a run of unknown machine ids -- a misconfigured imager, or someone
# posting junk at the open endpoint -- cannot grow this without bound. Well
# past any real fleet; when it is hit, the machines heard from least recently
# lose their rows first, which is the right ones.
MAX_HOSTS = 20000


def _dir() -> str:
    return os.path.join(settings.output_dir, "fleet")


def _hosts_path() -> str:
    return os.path.join(_dir(), "hosts.json")


def _state_path() -> str:
    return os.path.join(_dir(), "state.json")


def _write_atomic(path: str, payload: Any) -> None:
    """Write JSON so a crash mid-write cannot leave a half-file behind.

    Every store in this project that people can lose work in is written this
    way; the fleet's own state is the one place it would be tolerable to skip
    it, and it is not skipped because a truncated state.json would take the
    whole page down until someone deleted it by hand.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        # A corrupt or absent file reads as empty rather than raising. Same
        # rule as users.json: the fleet page showing nothing is recoverable,
        # the API refusing every request is not.
        return default
    return data if isinstance(data, type(default)) else default


class Fleet:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, dict[str, Any]] | None = None
        self._hosts: dict[str, Any] | None = None
        self._dirty = False
        self._flush_timer: threading.Timer | None = None
        # Group membership changes rarely and is resolved constantly: every
        # heartbeat asks which machines each candidate rollout covers, which is
        # a walk of every host per rollout per beat. A fleet of five hundred
        # with a handful of rollouts turns one check-in into thousands of
        # dictionary lookups, several hundred times a minute, to produce the
        # same answer every time.
        #
        # So results are cached against a counter that anything writing to the
        # host records bumps. A stale answer is not possible: the only inputs
        # are the host records and the state keys, and both bump it.
        self._epoch = 0
        self._members_cache: dict[tuple, list[str]] = {}

    # ---------------------------------------------------------------- loading

    def _ensure_loaded(self) -> None:
        if self._state is None:
            self._state = _read_json(_state_path(), {})
        if self._hosts is None:
            raw = _read_json(_hosts_path(), {})
            self._hosts = {"hosts": raw.get("hosts", {}) if isinstance(raw, dict) else {},
                           "groups": raw.get("groups", {}) if isinstance(raw, dict) else {}}

    def reload(self) -> None:
        """Drop the caches. Tests point settings.output_dir at a temp directory
        between cases, and a module-level singleton would otherwise carry one
        case's fleet into the next."""
        with self._lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
            self._state = None
            self._hosts = None
            self._dirty = False
            self._epoch += 1
            self._members_cache.clear()

    # --------------------------------------------------------------- flushing

    def _schedule_flush(self) -> None:
        self._dirty = True
        if self._flush_timer is not None:
            return
        self._flush_timer = threading.Timer(FLUSH_AFTER_SECONDS, self._flush)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _flush(self) -> None:
        with self._lock:
            self._flush_timer = None
            if not self._dirty or self._state is None:
                return
            snapshot = dict(self._state)
            self._dirty = False
        try:
            _write_atomic(_state_path(), snapshot)
        except OSError:
            # Losing a flush must never fail a heartbeat: a machine that cannot
            # report in is a machine that never gets its update.
            pass

    def flush_now(self) -> None:
        with self._lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
        self._flush()

    # ------------------------------------------------------------- heartbeats

    def heartbeat(self, ident: str, **fields: Any) -> dict[str, Any]:
        """Record one agent check-in and return the machine's merged record."""
        now = time.time()
        with self._lock:
            self._ensure_loaded()
            assert self._state is not None
            if ident not in self._state:
                self._epoch += 1
                self._members_cache.clear()
            rec = self._state.setdefault(ident, {"id": ident, "first_seen": now})
            rec["last_seen"] = now
            for key, value in fields.items():
                if value not in (None, ""):
                    rec[key] = value
            self._evict_if_huge()
            self._schedule_flush()
            return dict(rec)

    def _evict_if_huge(self) -> None:
        assert self._state is not None
        if len(self._state) <= MAX_HOSTS:
            return
        # Oldest heartbeats go first. A host with an operator-set record (a
        # group, a label) is kept regardless: someone deliberately wrote that
        # down, and it is not this cache's place to discard it.
        assert self._hosts is not None
        known = set(self._hosts["hosts"])
        victims = sorted((r.get("last_seen", 0), i) for i, r in self._state.items()
                         if i not in known)
        for _, ident in victims[: len(self._state) - MAX_HOSTS]:
            self._state.pop(ident, None)

    # ------------------------------------------------------------------ hosts

    def host(self, ident: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            assert self._hosts is not None
            return dict(self._hosts["hosts"].get(ident, {}))

    def set_host(self, ident: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            assert self._hosts is not None
            rec = self._hosts["hosts"].setdefault(ident, {})
            for key, value in fields.items():
                if value is None:
                    rec.pop(key, None)
                else:
                    rec[key] = value
            if not rec:
                self._hosts["hosts"].pop(ident, None)
            _write_atomic(_hosts_path(), self._hosts)
            self._epoch += 1
            self._members_cache.clear()
            return dict(rec)

    def groups(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            assert self._hosts is not None
            counts: dict[str, int] = {}
            for rec in self._hosts["hosts"].values():
                for g in rec.get("groups", []):
                    counts[g] = counts.get(g, 0) + 1
            out = {}
            for name, meta in self._hosts["groups"].items():
                out[name] = {**meta, "name": name, "hosts": counts.get(name, 0)}
            # A group that only exists because a host is in it is still a group.
            for name, n in counts.items():
                out.setdefault(name, {"name": name, "description": "", "hosts": n})
            return out

    def set_group(self, name: str, description: str = "") -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            assert self._hosts is not None
            self._hosts["groups"][name] = {"description": description}
            _write_atomic(_hosts_path(), self._hosts)
            self._epoch += 1
            self._members_cache.clear()
            return {"name": name, "description": description}

    def delete_group(self, name: str) -> int:
        """Remove a group and take every host out of it. Returns hosts touched."""
        with self._lock:
            self._ensure_loaded()
            assert self._hosts is not None
            self._hosts["groups"].pop(name, None)
            touched = 0
            for rec in self._hosts["hosts"].values():
                if name in rec.get("groups", []):
                    rec["groups"] = [g for g in rec["groups"] if g != name]
                    touched += 1
            _write_atomic(_hosts_path(), self._hosts)
            self._epoch += 1
            self._members_cache.clear()
            return touched

    def members(self, groups: list[str] | None = None, hosts: list[str] | None = None,
                everything: bool = False) -> list[str]:
        """Machine ids a rollout target resolves to, right now.

        Resolved live rather than frozen when the rollout is created: a machine
        imaged into `prod` on Tuesday should be picked up by Monday's still-open
        rollout, because otherwise the fleet drifts out of the state someone
        deliberately put it in and nothing says so.
        """
        with self._lock:
            self._ensure_loaded()
            assert self._hosts is not None and self._state is not None
            key = (self._epoch, tuple(sorted(groups or [])), tuple(sorted(hosts or [])),
                   everything)
            cached = self._members_cache.get(key)
            if cached is not None:
                return list(cached)
            if everything:
                out_list = sorted(set(self._state) | set(self._hosts["hosts"]))
            else:
                out: set[str] = set(hosts or [])
                for g in groups or []:
                    for ident, rec in self._hosts["hosts"].items():
                        if g in rec.get("groups", []):
                            out.add(ident)
                out_list = sorted(out)
            # Bounded by the number of distinct rollout targets, which is small;
            # cleared wholesale on any change rather than evicted, because the
            # epoch key would make every stale entry unreachable anyway.
            if len(self._members_cache) > 256:
                self._members_cache.clear()
            self._members_cache[key] = out_list
            return list(out_list)

    # ------------------------------------------------------------------ views

    def machines(self, interval: int) -> list[dict[str, Any]]:
        """One row per machine: reported state merged with operator metadata."""
        with self._lock:
            self._ensure_loaded()
            assert self._hosts is not None and self._state is not None
            now = time.time()
            rows = []
            for ident in set(self._state) | set(self._hosts["hosts"]):
                rec = dict(self._state.get(ident, {"id": ident}))
                rec["id"] = ident
                meta = self._hosts["hosts"].get(ident, {})
                rec["groups"] = meta.get("groups", [])
                rec["label"] = meta.get("label", "")
                rec["paused"] = bool(meta.get("paused"))
                rec["presence"] = presence(rec.get("last_seen"), interval, now)
                rows.append(rec)
            rows.sort(key=lambda r: r.get("last_seen") or 0, reverse=True)
            return rows


def presence(last_seen: float | None, interval: int, now: float | None = None) -> str:
    """online | stale | offline | unknown, from one timestamp.

    `unknown` is its own answer and not folded into `offline`: a machine that
    has never run an agent (imaged before this existed, or with the agent
    disabled) has not gone quiet, it was never speaking. Reporting those as
    offline would fill the page with alarms about machines that are fine.
    """
    if not last_seen:
        return "unknown"
    age = (now or time.time()) - last_seen
    if age <= interval * MISSED_BEATS_ONLINE:
        return "online"
    if age <= STALE_SECONDS:
        return "stale"
    return "offline"


fleet = Fleet()
