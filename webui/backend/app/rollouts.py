"""Staged rollouts: desired state the fleet pulls, rather than commands pushed at it.

An operator says "put group `prod` on bundle 1.4.0, one machine first, then ten
at a time, and stop if two fail". That intent is recorded here. Nothing is sent
anywhere. Each machine, on its own timer, asks this server what it should be
running; if the rollout has room for it right now, the answer names a bundle and
the machine fetches it. If it does not, the answer is "nothing to do".

Everything about that shape follows from one fact of the deployment: a machine
is imaged on a private provisioning switch and then moves somewhere this server
cannot reach it -- behind NAT, behind a firewall, on an address nobody wrote
down. A push-based design would work on the imaging bench and nowhere else.

The useful consequence is that there is no scheduler. A rollout does not tick;
it is evaluated when a machine asks, and the answer is a pure function of the
rollout's recorded state at that moment. Nothing has to run for a rollout to be
correct, and a server that was down for an hour resumes with no catch-up logic:
the machines simply ask again.

Rollout state per machine:

    pending     -> in the rollout, not yet told to do anything
    offered     -> told; has not yet said it started
    installing  -> agent reported it is downloading or installing
    rebooting   -> agent reported the install finished; awaiting the new boot
    verified    -> came back on the target version and passed its health check
    failed      -> the agent reported a failure, or it never progressed

`verified` is deliberately not "the agent said it installed". A bundle that
installs perfectly and then fails to boot is the exact failure the A/B layout
exists to survive, and a rollout that counted the install as success would
cheerfully march that bundle across the whole fleet while every machine quietly
rolled back. Success means the machine came back, on the new version, healthy.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime
from typing import Any

from .config import settings
from .fleet import _write_atomic, _read_json, fleet

# How long a machine may sit `offered` or `installing` before the rollout gives
# up waiting and offers the slot to somebody else. Generous: a large bundle over
# a slow link, on a machine that also has to reboot, is not a failure.
OFFER_TIMEOUT_SECONDS = 3600
# How many times one machine may be re-offered before it counts as failed. A
# machine that takes the offer and vanishes three times is not going to work.
MAX_ATTEMPTS = 3

TERMINAL = ("verified", "failed", "skipped")
IN_FLIGHT = ("offered", "installing", "rebooting")


def _path() -> str:
    return os.path.join(settings.output_dir, "fleet", "rollouts.json")


def _new_id() -> str:
    return "r-" + secrets.token_hex(4)


class Rollouts:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            raw = _read_json(_path(), {})
            self._data = raw if isinstance(raw, dict) else {}
        return self._data

    def _save(self) -> None:
        _write_atomic(_path(), self._data or {})

    def reload(self) -> None:
        with self._lock:
            self._data = None

    # ------------------------------------------------------------------ CRUD

    def create(self, *, bundle: str, version: str, bundle_url: str,
               groups: list[str], hosts: list[str], everything: bool,
               canary: int, batch_size: int, soak_seconds: int, max_failures: int,
               window: dict[str, Any] | None, created_by: str,
               description: str = "") -> dict[str, Any]:
        with self._lock:
            data = self._load()
            rid = _new_id()
            rec = {
                "id": rid,
                "bundle": bundle,
                "version": version,
                "bundle_url": bundle_url,
                "description": description,
                "target": {"groups": groups, "hosts": hosts, "all": everything},
                "strategy": {"canary": canary, "batch_size": batch_size,
                             "soak_seconds": soak_seconds, "max_failures": max_failures},
                "window": window,
                "state": "running",
                "halt_reason": "",
                "created": time.time(),
                "created_by": created_by,
                "machines": {},
            }
            data[rid] = rec
            self._save()
            return self.public(rid)

    def get(self, rid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load().get(rid)

    def set_state(self, rid: str, state: str, reason: str = "") -> dict[str, Any] | None:
        with self._lock:
            rec = self._load().get(rid)
            if rec is None:
                return None
            if state == "running":
                # Resuming means "I have looked at those failures; carry on with
                # the rest", so the budget starts counting again from here.
                # Without this, resuming a rollout that halted on its budget
                # re-halts on the very next heartbeat -- the failures are still
                # on the record, still over the limit -- and `resume` becomes a
                # button that returns 200 and does nothing, which is worse than
                # one that refuses.
                rec["failure_baseline"] = self._failures(rec)
            rec["state"] = state
            rec["halt_reason"] = reason
            self._save()
            return self.public(rid)

    def delete(self, rid: str) -> bool:
        with self._lock:
            data = self._load()
            if rid not in data:
                return False
            del data[rid]
            self._save()
            return True

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = sorted(self._load(), key=lambda r: self._load()[r].get("created", 0),
                         reverse=True)
            return [self.public(r) for r in ids]

    def public(self, rid: str) -> dict[str, Any]:
        """A rollout plus the counts an operator actually looks at."""
        with self._lock:
            rec = self._load()[rid]
            target = rec["target"]
            members = fleet.members(target["groups"], target["hosts"], target["all"])
            counts: dict[str, int] = {}
            for ident in members:
                st = rec["machines"].get(ident, {}).get("state", "pending")
                counts[st] = counts.get(st, 0) + 1
            done = counts.get("verified", 0) + counts.get("failed", 0) + counts.get("skipped", 0)
            return {**{k: v for k, v in rec.items() if k != "machines"},
                    "total": len(members),
                    "counts": counts,
                    "done": done,
                    "machines": {i: rec["machines"].get(i, {"state": "pending"})
                                 for i in members}}

    # ---------------------------------------------------------------- engine

    def _machine(self, rec: dict[str, Any], ident: str) -> dict[str, Any]:
        return rec["machines"].setdefault(ident, {"state": "pending", "attempts": 0})

    def _sweep_timeouts(self, rec: dict[str, Any], now: float) -> None:
        """Return abandoned offers to the pool so a rollout cannot wedge.

        Called on every evaluation rather than on a timer, for the same reason
        the rest of this has no scheduler: the only moment the answer matters is
        when a machine is asking, and at that moment it is cheap to work out.
        """
        for ident, m in rec["machines"].items():
            if m.get("state") not in IN_FLIGHT:
                continue
            if now - m.get("at", now) < OFFER_TIMEOUT_SECONDS:
                continue
            if m.get("attempts", 0) >= MAX_ATTEMPTS:
                m["state"] = "failed"
                m["error"] = (f"no progress after {m['attempts']} attempts; the machine "
                              "took the update and never reported back")
            else:
                m["state"] = "pending"
            m["at"] = now

    def _failures(self, rec: dict[str, Any]) -> int:
        return sum(1 for m in rec["machines"].values() if m.get("state") == "failed")

    def _capacity(self, rec: dict[str, Any], now: float) -> int:
        """How many more machines may start right now. Zero means "not yet"."""
        machines = rec["machines"].values()
        in_flight = sum(1 for m in machines if m.get("state") in IN_FLIGHT)
        verified = [m for m in machines if m.get("state") == "verified"]
        strategy = rec["strategy"]
        canary = max(0, int(strategy.get("canary", 0)))

        if len(verified) < canary:
            # Still proving the canaries. Never more than `canary` at once, and
            # a failed canary means the batch phase is never reached at all.
            return max(0, canary - in_flight)

        if canary and verified:
            # The soak: the canaries have to have been up for a while before the
            # rest of the fleet follows them. An update that bricks a machine
            # ten minutes in is still a bricked machine, and without this the
            # whole fleet would already have it.
            newest_canary = max(m.get("at", 0) for m in verified)
            soak = int(strategy.get("soak_seconds", 0))
            if now - newest_canary < soak:
                return 0

        return max(0, int(strategy.get("batch_size", 1)) - in_flight)

    def _in_window(self, rec: dict[str, Any], now: float) -> bool:
        """Is this rollout allowed to start machines at this moment?

        Server-local time on purpose. The alternative -- each machine deciding
        against its own clock -- means a maintenance window means different
        things on different machines, and the machine whose timezone is wrong
        is exactly the one nobody notices until it reboots mid-shift.
        """
        window = rec.get("window")
        if not window:
            return True
        moment = datetime.fromtimestamp(now)
        days = window.get("days")
        start, end = window.get("start", ""), window.get("end", "")
        if not start or not end:
            return True
        minutes = moment.hour * 60 + moment.minute

        def parse(text: str) -> int:
            hh, _, mm = text.partition(":")
            return int(hh) * 60 + int(mm or 0)

        try:
            s, e = parse(start), parse(end)
        except ValueError:
            return True
        # A window that wraps past midnight belongs to the day it *started* on,
        # so "Sat 22:00-04:00" still permits work at 01:00 on Sunday morning.
        if s <= e:
            return (days is None or moment.weekday() in days) and s <= minutes < e
        if minutes >= s:
            return days is None or moment.weekday() in days
        return days is None or ((moment.weekday() - 1) % 7) in days

    def evaluate(self, ident: str, reported: dict[str, Any]) -> dict[str, Any] | None:
        """Fold one heartbeat into whatever rollout owns this machine, and say
        what the machine should do next. None means "nothing to do"."""
        now = time.time()
        with self._lock:
            data = self._load()
            rec = self._owning_rollout(data, ident)
            if rec is None:
                return None

            m = self._machine(rec, ident)
            changed = self._apply_report(rec, m, ident, reported, now)

            action = None
            if rec["state"] == "running" and m["state"] == "pending" \
                    and not fleet.host(ident).get("paused"):
                self._sweep_timeouts(rec, now)
                if self._in_window(rec, now) and self._capacity(rec, now) > 0:
                    m["state"] = "offered"
                    m["at"] = now
                    m["attempts"] = m.get("attempts", 0) + 1
                    m.pop("error", None)
                    changed = True
                    action = {"type": "update", "rollout": rec["id"],
                              "bundle_url": rec["bundle_url"], "version": rec["version"]}

            budget = int(rec["strategy"].get("max_failures", 0))
            since_resume = self._failures(rec) - int(rec.get("failure_baseline", 0))
            if budget > 0 and since_resume >= budget and rec["state"] == "running":
                rec["state"] = "halted"
                rec["halt_reason"] = (f"{since_resume} machines failed; "
                                      "the rollout stopped on its own")
                changed = True
                action = None      # nothing further goes out under a halted rollout

            if rec["state"] == "running" and self._complete(rec):
                rec["state"] = "completed"
                changed = True

            if changed:
                self._save()
            return action

    def _complete(self, rec: dict[str, Any]) -> bool:
        target = rec["target"]
        members = fleet.members(target["groups"], target["hosts"], target["all"])
        if not members:
            return False
        return all(rec["machines"].get(i, {}).get("state") in TERMINAL for i in members)

    def _owning_rollout(self, data: dict[str, Any], ident: str) -> dict[str, Any] | None:
        """Which rollout, if any, this machine is currently answering to.

        A machine that is mid-install keeps answering to the rollout that gave
        it the bundle even if a newer one has since been created -- interrupting
        an install to start a different one is how a machine ends up on neither
        version. Otherwise the newest running rollout that names it wins.
        """
        candidates = sorted(data.values(), key=lambda r: r.get("created", 0), reverse=True)
        for rec in candidates:
            state = rec["machines"].get(ident, {}).get("state")
            if state in IN_FLIGHT:
                return rec
        for rec in candidates:
            if rec["state"] not in ("running", "paused"):
                continue
            target = rec["target"]
            if ident not in fleet.members(target["groups"], target["hosts"], target["all"]):
                continue
            if rec["machines"].get(ident, {}).get("state") in TERMINAL:
                continue
            return rec
        return None

    def _apply_report(self, rec: dict[str, Any], m: dict[str, Any], ident: str,
                      reported: dict[str, Any], now: float) -> bool:
        """Move this machine's rollout state on from what the agent just said."""
        said = str(reported.get("update_state") or "").strip()
        version = str(reported.get("version") or "").strip()
        before = dict(m)

        # Success, defined as the machine coming back rather than as the install
        # returning zero -- see the module docstring.
        if m["state"] in ("rebooting", "installing", "offered") and version == rec["version"]:
            if str(reported.get("health") or "ok") == "ok":
                m["state"] = "verified"
                m["at"] = now
                m.pop("error", None)
                return True

        if said in ("downloading", "installing"):
            m["state"] = "installing"
            m["at"] = now
        elif said == "installed":
            m["state"] = "rebooting"
            m["at"] = now
        elif said == "failed":
            m["state"] = "failed"
            m["at"] = now
            m["error"] = str(reported.get("update_error") or "the agent reported a failure")

        return m != before


rollouts = Rollouts()
