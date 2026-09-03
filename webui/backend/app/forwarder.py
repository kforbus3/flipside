"""Ship audit events off the box, to syslog or an HTTP collector.

An audit log that only exists on the machine being audited is the one an
attacker deletes, and the one that goes with the disk when the disk goes. It is
also bounded and trimmed oldest-first here, so a busy month quietly erases the
records of the month before it. Both problems have the same answer: send each
event somewhere else as it happens, and let that somewhere else be the system of
record.

Two properties this has to have, and they pull against each other:

  Never block. The forwarder sits behind `audit.record()`, which sits behind
  every mutating API call. A SIEM that is slow, wedged, or gone must not make
  this server slow, wedged, or gone with it -- an audit trail that can stall the
  work it audits is a worse availability problem than a gap in the trail.

  Never silently drop everything. "Nothing is arriving" and "nothing happened"
  look identical at the collector, so failures are counted and surfaced, and the
  queue's high-water mark is visible in /metrics rather than only in a log
  nobody is reading.

So: a bounded queue and one background thread. When the queue is full, the
oldest event is dropped and a counter goes up -- dropping the newest would mean
losing exactly the burst of activity most worth having, and blocking would mean
the first property is not true.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from app.config import settings

# Deep enough to ride out a collector restart at any plausible event rate;
# shallow enough that a permanently dead collector costs a few megabytes rather
# than the machine.
QUEUE_DEPTH = 10000
HTTP_TIMEOUT = 5
# How long to wait after a failure before trying again, so a dead collector is
# not retried once per event. Doubles to the cap.
BACKOFF_START = 2.0
BACKOFF_MAX = 60.0

# RFC 5424 facility 13 (log audit) x 8 + severity 6 (informational).
_PRI = 13 * 8 + 6


class Stats:
    """What the forwarder is doing, for /metrics and the Audit page.

    Kept as plain counters read without a lock: they are only ever incremented
    by the single forwarder thread and read for display, and a torn read of an
    integer counter is not a problem worth a lock on the hot path.
    """

    def __init__(self) -> None:
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.queued = 0
        self.last_error = ""
        self.last_success = 0.0


stats = Stats()


class Forwarder:
    def __init__(self) -> None:
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=QUEUE_DEPTH)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle

    def enabled(self) -> bool:
        return bool(settings.audit_syslog or settings.audit_http_url)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="audit-forwarder",
                                            daemon=True)
            self._thread.start()

    def submit(self, row: dict[str, Any]) -> None:
        """Queue one event. Returns immediately, always."""
        if not self.enabled():
            return
        self._ensure_thread()
        try:
            self._q.put_nowait(row)
        except queue.Full:
            # Make room by discarding the oldest rather than refusing the
            # newest: a full queue means the collector has been unreachable for
            # a while, and the events worth keeping are the recent ones.
            try:
                self._q.get_nowait()
                stats.dropped += 1
                self._q.put_nowait(row)
            except (queue.Empty, queue.Full):
                stats.dropped += 1
        stats.queued = self._q.qsize()

    # ----------------------------------------------------------------- worker

    def _run(self) -> None:
        backoff = 0.0
        while True:
            row = self._q.get()
            stats.queued = self._q.qsize()
            if backoff:
                time.sleep(backoff)
            try:
                self._deliver(row)
                stats.sent += 1
                stats.last_success = time.time()
                backoff = 0.0
            except Exception as exc:                       # noqa: BLE001
                # Any failure at all is the collector's problem, not this
                # server's. Recorded, backed off, and dropped -- retrying one
                # event forever would stall every event behind it.
                stats.failed += 1
                stats.last_error = f"{type(exc).__name__}: {exc}"[:200]
                backoff = min(BACKOFF_MAX, max(BACKOFF_START, backoff * 2))

    def _deliver(self, row: dict[str, Any]) -> None:
        if settings.audit_syslog:
            self._to_syslog(row)
        if settings.audit_http_url:
            self._to_http(row)

    # ------------------------------------------------------------- transports

    def _to_syslog(self, row: dict[str, Any]) -> None:
        """RFC 5424 with the event as a JSON message.

        Structured data would be more correct and is much less useful: every
        collector parses a JSON message body, and half of them mangle SD-PARAMs.
        The syslog header carries the routing, the JSON carries the event.
        """
        target = settings.audit_syslog
        proto, _, address = target.partition("://")
        if not address:
            proto, address = "udp", target
        host, _, port = address.partition(":")
        port_n = int(port or 514)

        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row.get("ts", time.time())))
        hostname = settings.audit_syslog_hostname or socket.gethostname()
        message = (f"<{_PRI}>1 {timestamp} {hostname} flipside - - - "
                   + json.dumps(row, separators=(",", ":")))
        payload = message.encode("utf-8", "replace")

        if proto == "tcp":
            with socket.create_connection((host, port_n), timeout=HTTP_TIMEOUT) as sock:
                # Octet counting (RFC 6587). Newline framing splits an event
                # whose message contains one, and these carry free text.
                sock.sendall(f"{len(payload)} ".encode() + payload)
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(HTTP_TIMEOUT)
                # UDP syslog silently truncates past the receiver's buffer, so
                # keep a datagram inside the conservative 1 KiB every
                # implementation accepts rather than discovering the limit as
                # half-events at the far end.
                sock.sendto(payload[:1024], (host, port_n))

    def _to_http(self, row: dict[str, Any]) -> None:
        data = json.dumps(row).encode()
        req = urllib.request.Request(settings.audit_http_url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if settings.audit_http_token:
            req.add_header("Authorization", f"Bearer {settings.audit_http_token}")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status >= 300:
                raise urllib.error.HTTPError(settings.audit_http_url, resp.status,
                                             "collector refused the event", resp.headers, None)


forwarder = Forwarder()
