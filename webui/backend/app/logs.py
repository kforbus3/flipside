"""Logging that a collector can read, without making the console worse to use.

Two audiences want opposite things from a log line. Someone running
`make webui-logs` to watch a build wants prose. A log shipper wants one JSON
object per line with stable field names and no wrapped tracebacks. Neither is
the right default for the other, so the format is a setting and prose stays the
default -- turning on JSON is the deliberate act of someone who has somewhere to
send it.

Applied to uvicorn's own loggers as well as ours. Configuring only the
application logger produces the worst of both: structured application lines
interleaved with uvicorn's human-formatted access log, which is neither
parseable nor readable, and which looks like it works right up until a query
across it returns half the requests.
"""

from __future__ import annotations

import json
import logging
import time


class JSONFormatter(logging.Formatter):
    """One object per line. Extra fields set on the record are merged in."""

    # Everything LogRecord defines. Anything else on the record was put there by
    # a caller passing extra=, and belongs in the output — enumerating the ones
    # we know about instead would silently drop fields added later.
    _STANDARD = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            # As one string field rather than as wrapped lines: a traceback
            # spread over forty lines becomes forty log events at the collector,
            # thirty-nine of which are unparseable and none of which are the
            # error.
            payload["exception"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str)
        except (TypeError, ValueError):
            # A log line must never be the thing that raises. Fall back to the
            # message alone rather than losing the event entirely.
            return json.dumps({"ts": payload["ts"], "level": payload["level"],
                               "logger": payload["logger"], "msg": payload["msg"]})


def configure(json_output: bool, level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)

    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter() if json_output else logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))

    for logger in (root, logging.getLogger("uvicorn"),
                   logging.getLogger("uvicorn.error"),
                   logging.getLogger("uvicorn.access")):
        logger.handlers = [handler]
        logger.setLevel(numeric)
        # uvicorn's loggers propagate to root by default, which with a handler
        # on both prints every line twice.
        logger.propagate = logger is root


log = logging.getLogger("flipside")
