"""Counters this server keeps, and the Prometheus rendering of them.

Two kinds of number live here and they are gathered differently.

Cumulative counters -- requests, logins, heartbeats, rollout offers -- are
incremented in-process as things happen. They reset when the process restarts,
which is exactly what a Prometheus counter is defined to do; `rate()` handles it.

Gauges -- how many machines are online, how many images exist, free disk -- are
read at scrape time from the stores that already hold the truth. Keeping a
mirrored copy in memory would be a second source that drifts from the first
without saying so, and the read is a few file stats.

No client library. Adding prometheus_client to pull in one text format is a
dependency in every deployment for about forty lines of string building, and
the exposition format is stable and small enough to write out.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from typing import Iterable

from app import __version__
from app.config import settings

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

START = time.time()

# Metric help and type, declared once so the exposition is well-formed even for
# a counter that has not been incremented yet this run.
_DECLARED: dict[str, tuple[str, str]] = {
    "flipside_http_requests_total": ("counter", "API requests, by method and status class"),
    "flipside_login_attempts_total": ("counter", "Login attempts, by outcome"),
    "flipside_heartbeats_total": ("counter", "Agent check-ins received"),
    "flipside_rollout_offers_total": ("counter", "Updates offered to machines by a rollout"),
    "flipside_rollout_results_total": ("counter", "Machines a rollout finished with, by outcome"),
    "flipside_jobs_total": ("counter", "Builder jobs run, by type and outcome"),
}


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    """Add to a counter. Never raises: a metric must not be able to fail a
    request, which is the only reason this is worth a wrapper at all."""
    try:
        key = (name, tuple(sorted(labels.items())))
        with _lock:
            _counters[key] = _counters.get(key, 0.0) + value
    except Exception:                                     # noqa: BLE001
        pass


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _line(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if labels:
        rendered = ",".join(f'{k}="{_escape(str(v))}"' for k, v in sorted(labels.items()) if v != "")
        if rendered:
            return f"{name}{{{rendered}}} {value}"
    return f"{name} {value}"


def _gauges() -> Iterable[tuple[str, str, str, list[tuple[dict[str, str], float]]]]:
    """(name, type, help, [(labels, value)]) read fresh at scrape time."""
    from app import orchestrator as orch
    from app.deployments import deployments
    from app.fleet import fleet
    from app.forwarder import stats as fwd_stats
    from app.jobs import jobs
    from app.rollouts import rollouts

    yield ("flipside_up", "gauge", "Always 1; carries the build version as a label",
           [({"version": __version__}, 1)])
    yield ("flipside_uptime_seconds", "gauge", "Seconds since this process started",
           [({}, round(time.time() - START, 1))])

    # --- the fleet -----------------------------------------------------------
    machines = fleet.machines(settings.agent_interval)
    presence: dict[str, int] = {}
    for row in machines:
        presence[row["presence"]] = presence.get(row["presence"], 0) + 1
    for state in ("online", "stale", "offline", "unknown"):
        presence.setdefault(state, 0)
    yield ("flipside_fleet_machines", "gauge", "Machines known, by presence",
           [({"presence": k}, v) for k, v in sorted(presence.items())])

    # Version cardinality is bounded by how many builds are actually deployed,
    # which is small -- but it is fed by machines, and a machine reporting junk
    # would otherwise create a time series per lie. Only the ten commonest are
    # exported; the rest are summed into one series so the total still adds up.
    versions: dict[str, int] = {}
    for row in machines:
        if row.get("version") and row["presence"] in ("online", "stale"):
            versions[row["version"]] = versions.get(row["version"], 0) + 1
    ranked = sorted(versions.items(), key=lambda kv: kv[1], reverse=True)
    series = [({"version": v}, n) for v, n in ranked[:10]]
    if len(ranked) > 10:
        series.append(({"version": "other"}, sum(n for _, n in ranked[10:])))
    yield ("flipside_fleet_version_machines", "gauge",
           "Machines running each version (top 10, remainder as 'other')", series)

    degraded = sum(1 for r in machines if r.get("health") == "degraded")
    yield ("flipside_fleet_degraded", "gauge",
           "Machines whose last check-in reported a failed unit", [({}, degraded)])

    provisioned = deployments.fleet()
    yield ("flipside_never_booted", "gauge",
           "Machines that finished imaging and never reported booting",
           [({}, sum(1 for m in provisioned if m.get("state") == "never-booted"))])

    # --- rollouts ------------------------------------------------------------
    by_state: dict[str, int] = {}
    progress: list[tuple[dict[str, str], float]] = []
    for rec in rollouts.list():
        by_state[rec["state"]] = by_state.get(rec["state"], 0) + 1
        if rec["state"] in ("running", "paused", "halted"):
            for phase, n in rec["counts"].items():
                progress.append(({"rollout": rec["id"], "version": rec["version"],
                                  "phase": phase}, n))
    for state in ("running", "paused", "halted", "completed", "cancelled"):
        by_state.setdefault(state, 0)
    yield ("flipside_rollouts", "gauge", "Rollouts, by state",
           [({"state": k}, v) for k, v in sorted(by_state.items())])
    # Only live rollouts, so a year of completed ones does not accumulate series
    # forever. A halted rollout stays visible because it is the one to alert on.
    yield ("flipside_rollout_machines", "gauge",
           "Machines in each live rollout, by phase", progress)

    # --- artifacts and the disk they live on ---------------------------------
    try:
        images, _ = orch.list_images()
        yield ("flipside_images", "gauge", "Built images in the library",
               [({}, len(images))])
        yield ("flipside_images_bytes", "gauge", "Total size of the image library",
               [({}, sum(i["size"] for i in images))])
    except OSError:
        pass
    try:
        bundles = orch.list_bundles()
        yield ("flipside_bundles", "gauge", "Update bundles available",
               [({}, len(bundles))])
    except OSError:
        pass
    try:
        usage = shutil.disk_usage(settings.output_dir)
        yield ("flipside_disk_free_bytes", "gauge",
               "Free space where images and bundles are written", [({}, usage.free)])
        yield ("flipside_disk_total_bytes", "gauge",
               "Total space where images and bundles are written", [({}, usage.total)])
    except OSError:
        # The one gauge worth alerting on is the one that disappears when the
        # path it measures does — so its absence is louder than a zero.
        pass

    # --- jobs ----------------------------------------------------------------
    running: dict[str, int] = {}
    for job in jobs.list():
        if job.get("status") == "running":
            running[job.get("type", "?")] = running.get(job.get("type", "?"), 0) + 1
    yield ("flipside_jobs_running", "gauge", "Builder jobs running now, by type",
           [({"type": k}, v) for k, v in sorted(running.items())] or [({}, 0)])

    # --- the audit forwarder --------------------------------------------------
    yield ("flipside_audit_queue", "gauge",
           "Audit events waiting to be shipped off the box", [({}, fwd_stats.queued)])
    yield ("flipside_audit_forwarded_total", "counter",
           "Audit events accepted by the collector", [({}, fwd_stats.sent)])
    yield ("flipside_audit_failed_total", "counter",
           "Deliveries the collector refused or timed out", [({}, fwd_stats.failed)])
    yield ("flipside_audit_dropped_total", "counter",
           "Audit events discarded because the collector was unreachable for too long",
           [({}, fwd_stats.dropped)])
    yield ("flipside_audit_last_success_seconds", "gauge",
           "Unix time of the last audit event accepted by the collector "
           "(0 if none yet; alert on this rather than on the queue, which is "
           "empty both when everything is fine and when nothing is being sent)",
           [({}, round(fwd_stats.last_success, 0))])


def render() -> str:
    out: list[str] = []
    for name, kind, help_text, series in _gauges():
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        for labels, value in series:
            out.append(_line(name, value, labels))

    with _lock:
        snapshot = dict(_counters)
    seen: set[str] = set()
    for name, (kind, help_text) in _DECLARED.items():
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        rows = [(dict(lbls), v) for (n, lbls), v in snapshot.items() if n == name]
        if not rows:
            # Declared but never incremented. Emitting nothing would make a
            # dashboard show "no data" for a counter that is legitimately zero,
            # which reads as a broken exporter rather than a quiet server.
            out.append(_line(name, 0))
        for labels, value in sorted(rows, key=lambda r: sorted(r[0].items())):
            out.append(_line(name, value, labels))
        seen.add(name)
    # Anything incremented without a declaration still gets exported rather than
    # silently dropped -- a metric added in a hurry should show up.
    for (name, lbls), value in sorted(snapshot.items()):
        if name not in seen:
            out.append(_line(name, value, dict(lbls)))
    return "\n".join(out) + "\n"


# Path segments that are route names rather than identifiers. Anything not on
# this list, in a position where an identifier can appear, becomes ":id".
_ROUTE_WORDS = {
    "api", "auth", "login", "logout", "check", "methods", "oidc", "callback",
    "exchange", "images", "disk", "download", "bundles", "build", "jobs",
    "stream", "cancel", "logs", "server", "config", "assignments", "interfaces",
    "preflight", "status", "up", "down", "imaging", "report", "checkin",
    "deployments", "fleet", "heartbeat", "hosts", "groups", "enrollment",
    "rollouts", "pause", "resume", "cancel", "secrets", "reveal", "test",
    "users", "tokens", "sessions", "audit", "overlay", "files", "sbom",
    "updates", "latest", "health", "metrics", "version",
}


def path_bucket(path: str) -> str:
    """A low-cardinality label for a request path.

    Never the raw path. It carries machine ids, image filenames and rollout
    ids, and one time series per machine id is how a metrics endpoint takes
    down the Prometheus scraping it -- a fleet of five hundred checking in
    would mint five hundred series from this one label. Identifier-shaped
    segments collapse to ":id", which keeps "the fleet is checking in"
    distinguishable from "someone is hammering login" at a fixed cost.
    """
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] != "api":
        return "other"
    out = []
    for segment in parts[:4]:
        out.append(segment if segment.lower() in _ROUTE_WORDS else ":id")
    return "/" + "/".join(out)
