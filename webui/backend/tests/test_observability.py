#!/usr/bin/env python3
"""Metrics, structured logs, and getting the audit trail off the box.

Three failures, each of which looks like success:

  - a /metrics endpoint whose labels come from user data. One time series per
    machine id is how a metrics endpoint takes down the Prometheus scraping it,
    and a fleet of five hundred checking in every five minutes is exactly the
    shape that does it. The exporter looks fine; the monitoring system falls
    over a week later.
  - a JSON log line that is not JSON, or a traceback spread across forty lines,
    each of which the collector stores as its own unparseable event.
  - an audit forwarder that blocks. It sits behind every mutating API call, so a
    SIEM that is wedged would wedge the server with it — an audit trail that can
    stall the work it audits is worse than a gap in the trail.

The forwarder is exercised against a real socket and a real HTTP server rather
than a mock, because what is being tested is that it survives them being slow
and being absent.
"""
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time

PROJ = tempfile.mkdtemp()
os.makedirs(os.path.join(PROJ, "output"), exist_ok=True)
os.environ.update(PROJECT_DIR=PROJ, STATIC_DIR="/tmp/none",
                  ADMIN_PASSWORD="ci-pw", SECRET_KEY="ci-secret")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fastapi.testclient import TestClient          # noqa: E402
from app.main import app                           # noqa: E402
from app import metrics, forwarder as fwd_mod      # noqa: E402
from app.config import settings                    # noqa: E402
from app.logs import JSONFormatter                 # noqa: E402

client = TestClient(app)
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name} {extra}")


def parse(text):
    """The exposition format, as {(name, frozenset(labels)): value}."""
    out, types = {}, {}
    for line in text.splitlines():
        if line.startswith("# TYPE"):
            _, _, name, kind = line.split()
            types[name] = kind
            continue
        if line.startswith("#") or not line.strip():
            continue
        left, _, value = line.rpartition(" ")
        name, _, labels = left.partition("{")
        pairs = frozenset()
        if labels:
            pairs = frozenset(
                (k, v.strip('"')) for k, _, v in
                (p.partition("=") for p in labels.rstrip("}").split(",")) if k)
        out[(name, pairs)] = float(value)
    return out, types


TOKEN = client.post("/api/auth/login",
                    data={"username": "admin", "password": "ci-pw"}).json()["access_token"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}

print("== /metrics is where Prometheus looks for it, and needs a credential ==")
check("unauthenticated scrape is refused", client.get("/metrics").status_code == 401)
r = client.get("/metrics", headers=AUTH)
check("an authenticated one works", r.status_code == 200, r.status_code)
check("with the content type Prometheus expects",
      "version=0.0.4" in r.headers["content-type"], r.headers.get("content-type"))
check("and it is also reachable under /api", client.get("/api/metrics", headers=AUTH).status_code == 200)

series, types = parse(r.text)
check("every series has a declared type",
      all(name in types for name, _ in series), sorted({n for n, _ in series} - set(types)))
check("the build version is exported", any(n == "flipside_up" for n, _ in series))
check("presence is exported for all four states, including zeroes",
      sum(1 for n, lb in series if n == "flipside_fleet_machines") == 4,
      [lb for n, lb in series if n == "flipside_fleet_machines"])
# A counter that has never fired must still appear. Emitting nothing makes a
# dashboard read "no data", which looks like a broken exporter rather than a
# quiet server.
check("a counter at zero is still exported",
      ("flipside_login_attempts_total", frozenset()) in series
      or any(n == "flipside_login_attempts_total" for n, _ in series),
      [n for n, _ in series if "login" in n])

print("== label cardinality is bounded by routes, never by user data ==")
# The failure this prevents: /api/fleet/hosts/<mac> as a label mints one time
# series per machine, and the fleet is the largest source of requests here.
for mac in ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:03"):
    client.post("/api/fleet/heartbeat", data={"id": mac})
    client.put(f"/api/fleet/hosts/{mac}", json={"label": "x"}, headers=AUTH)
series, _ = parse(client.get("/metrics", headers=AUTH).text)
paths = {dict(lb).get("path") for n, lb in series if n == "flipside_http_requests_total"}
check("no machine id reaches a metric label",
      not any("aa:bb" in (p or "") for p in paths), paths)
check("the host route is bucketed to one series",
      "/api/fleet/hosts/:id" in paths, paths)
check("and the heartbeat keeps its own", "/api/fleet/heartbeat" in paths, paths)
check("heartbeats are counted",
      series.get(("flipside_heartbeats_total", frozenset()), 0) >= 3,
      series.get(("flipside_heartbeats_total", frozenset())))

# Directly, because a bucket that happens to be right for today's routes is
# not the same as one that is right by construction.
check("an image filename is bucketed away",
      metrics.path_bucket("/api/sbom/debian-trixie-ab.img.zst") == "/api/sbom/:id",
      metrics.path_bucket("/api/sbom/debian-trixie-ab.img.zst"))
check("a rollout id too",
      metrics.path_bucket("/api/rollouts/r-deadbeef/pause") == "/api/rollouts/:id/pause",
      metrics.path_bucket("/api/rollouts/r-deadbeef/pause"))
check("non-API paths collapse to one bucket",
      metrics.path_bucket("/assets/index-abc123.js") == "other")

print("== login outcomes are counted, which is what an alert reads ==")
client.post("/api/auth/login", data={"username": "admin", "password": "wrong"})
series, _ = parse(client.get("/metrics", headers=AUTH).text)
check("a failed login increments the failure counter",
      series.get(("flipside_login_attempts_total", frozenset({("outcome", "failure")}))) == 1,
      [(lb, v) for (n, lb), v in series.items() if n == "flipside_login_attempts_total"])
check("and successes are counted separately",
      series.get(("flipside_login_attempts_total", frozenset({("outcome", "success")}))) >= 1)

print("== a label value that would break the format is escaped ==")
check("quotes and backslashes are escaped",
      metrics._line("m", 1, {"v": 'a"b\\c'}) == 'm{v="a\\"b\\\\c"} 1',
      metrics._line("m", 1, {"v": 'a"b\\c'}))
check("newlines cannot inject a second sample",
      "\n" not in metrics._line("m", 1, {"v": "a\nb"}),
      metrics._line("m", 1, {"v": "a\nb"}))

print("== JSON logging produces one parseable object per event ==")
rec = logging.LogRecord("flipside", logging.INFO, __file__, 1, "hello", None, None)
rec.actor = "admin"
rec.duration_ms = 12.5
line = JSONFormatter().format(rec)
obj = json.loads(line)
check("the line is valid JSON", isinstance(obj, dict))
check("extra fields are merged in", obj["actor"] == "admin" and obj["duration_ms"] == 12.5, obj)
check("standard record noise is not", "pathname" not in obj and "msecs" not in obj, sorted(obj))
try:
    raise ValueError("boom")
except ValueError:
    rec2 = logging.LogRecord("flipside", logging.ERROR, __file__, 1, "failed", None,
                             sys.exc_info())
line2 = JSONFormatter().format(rec2)
check("an exception is one event, not forty",
      len(line2.splitlines()) == 1, len(line2.splitlines()))
obj2 = json.loads(line2)
check("with the traceback in a field", "boom" in obj2["exception"], obj2.get("exception"))
# A log line must never be the thing that raises.
rec3 = logging.LogRecord("flipside", logging.INFO, __file__, 1, "x", None, None)
rec3.weird = object()
check("an unserialisable extra still produces a line",
      json.loads(JSONFormatter().format(rec3))["msg"] == "x")

print("== the audit forwarder ships events without ever blocking ==")
received: list[bytes] = []
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("127.0.0.1", 0))
sock.settimeout(5)
port = sock.getsockname()[1]

settings.audit_syslog = f"udp://127.0.0.1:{port}"
try:
    from app import audit
    audit.record(actor="admin", role="admin", method="POST", path="/api/rollouts",
                 status=200, ip="10.0.0.9", summary="rollout r-1 created")
    try:
        payload = sock.recvfrom(4096)[0].decode()
    except socket.timeout:
        payload = ""
    check("the event reaches a syslog listener", payload != "", "nothing arrived")
    if payload:
        check("as RFC 5424 with a priority and the app name",
              payload.startswith("<110>1 ") and " flipside " in payload, payload[:80])
        body = payload[payload.index("{"):]
        event = json.loads(body)
        check("carrying the event as JSON",
              event["actor"] == "admin" and event["path"] == "/api/rollouts", event)
        check("and the summary an operator would search for",
              event.get("summary") == "rollout r-1 created", event)
finally:
    sock.close()
    settings.audit_syslog = ""

print("== a collector that hangs does not stall the server ==")
# The property that matters, and it has to be tested against a collector that
# *hangs* rather than one that refuses. A refused connection returns instantly,
# so pointing at a closed port would pass whether the forwarder were
# asynchronous or not -- a test that cannot fail for the reason it exists.
#
# This listener accepts and then says nothing, so each synchronous delivery
# would cost the full HTTP timeout. Ten events synchronously is fifty seconds;
# asynchronously it is the cost of ten queue puts.
hang = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
hang.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
hang.bind(("127.0.0.1", 0))
hang.listen(64)
hang_port = hang.getsockname()[1]
accepted = []


def _swallow():
    while True:
        try:
            conn, _ = hang.accept()
        except OSError:
            return
        accepted.append(conn)      # held open, never answered


threading.Thread(target=_swallow, daemon=True).start()

settings.audit_http_url = f"http://127.0.0.1:{hang_port}/collect"
try:
    check("the timeout this relies on is long enough to be conclusive",
          fwd_mod.HTTP_TIMEOUT >= 2, fwd_mod.HTTP_TIMEOUT)
    started = time.monotonic()
    for i in range(10):
        r = client.put(f"/api/fleet/hosts/hang{i}", json={"label": "x"}, headers=AUTH)
    elapsed = time.monotonic() - started
    check("ten audited calls against a hanging collector stay fast",
          elapsed < fwd_mod.HTTP_TIMEOUT, f"took {elapsed:.1f}s, "
          f"one synchronous delivery alone would be {fwd_mod.HTTP_TIMEOUT}s")
    check("and the requests succeeded anyway", r.status_code == 200, r.status_code)
    check("the audit line was still written locally",
          any(e.get("path", "").endswith("hang9")
              for e in client.get("/api/audit", headers=AUTH).json()["events"]))
finally:
    settings.audit_http_url = ""
    hang.close()
    for c in accepted:
        c.close()

print("== a full queue drops the oldest and says so ==")
# Not the newest: a full queue means the collector has been unreachable for a
# while, and the events worth keeping are the recent ones.
settings.audit_syslog = "udp://127.0.0.1:9"
try:
    f = fwd_mod.Forwarder()
    f._q = __import__("queue").Queue(maxsize=3)
    before = fwd_mod.stats.dropped
    for i in range(10):
        f._q.put_nowait({"n": i}) if f._q.qsize() < 3 else f.submit({"n": i})
    check("overflow is counted rather than silently discarded",
          fwd_mod.stats.dropped > before, (before, fwd_mod.stats.dropped))
    remaining = [f._q.get_nowait()["n"] for _ in range(f._q.qsize())]
    check("and it is the oldest that goes", remaining[-1] == 9, remaining)
finally:
    settings.audit_syslog = ""

print("== forwarding is visible, so silence can be told from success ==")
r = client.get("/api/metrics.json", headers=AUTH).json()
check("the JSON view reports forwarder state",
      "audit_forwarding" in r and "dropped" in r["audit_forwarding"], r.keys())
series, _ = parse(client.get("/metrics", headers=AUTH).text)
check("and /metrics carries a last-success timestamp to alert on",
      any(n == "flipside_audit_last_success_seconds" for n, _ in series))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
