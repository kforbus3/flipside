"""FastAPI app: API under /api, built SPA at /."""

from __future__ import annotations

import os
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import importlib
import pkgutil

from fastapi import Request

from app import __version__
from app import audit as _audit
from app import logs as _logs
from app import metrics as _metrics
from app import routers as _routers
from app.config import settings as _settings

_logs.configure(_settings.log_json, _settings.log_level)

STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(os.path.dirname(__file__), "..", "static"))

# The SPA is served same-origin (and the vite dev server proxies /api), so no
# CORS policy is needed — browsers then refuse cross-origin API use outright.
app = FastAPI(title="Flipside UI", version=__version__)


# Paths where the middleware below must not write audit entries: the machine
# endpoints (a fleet imaging itself would flood the log with rows that say
# nothing about people), and the two login-shaped POSTs, which their routers
# record themselves with the attempted username — the middleware only ever
# sees "anonymous 401" (and for the SSO exchange, the OIDC callback already
# wrote the real login line).
#
# The agent heartbeat is the load-bearing exclusion now. It is not an occasional
# machine event like the imaging pair: five hundred machines on a five-minute
# timer is a hundred and forty thousand rows a day, and the audit log is bounded
# and trimmed oldest-first — so auditing it would quietly evict every record of
# what people did, which is the only thing the log is for. What an operator did
# to the fleet is audited where it happens (/api/rollouts, /api/fleet/hosts);
# what a machine said about itself is fleet state, and lives in state.json.
_UNAUDITED = {"/api/imaging/report", "/api/imaging/checkin", "/api/auth/login",
              "/api/auth/oidc/exchange", "/api/fleet/heartbeat"}


@app.middleware("http")
async def audit_mutations(request: Request, call_next):
    """One audit line per non-GET API call, written after the response exists.

    Middleware rather than per-endpoint calls, for the same reason every
    router is auto-mounted below: a hand-maintained list is the thing that
    drifts. A new mutating endpoint is audited the day it is written. The
    principal is stashed on request.state by the auth dependency; a request
    that never authenticated is recorded as anonymous, which for a mutating
    call is worth a line too — that is someone knocking.
    """
    started = time.monotonic()
    response = await call_next(request)
    # Counted for every request, audited for very few. The label is a bucketed
    # path, never the real one: it carries machine ids and image names, and one
    # time series per machine id is how a /metrics endpoint takes down the
    # Prometheus scraping it (see metrics.path_bucket).
    _metrics.inc("flipside_http_requests_total",
                 method=request.method,
                 path=_metrics.path_bucket(request.url.path),
                 status=f"{response.status_code // 100}xx")
    if request.url.path == "/api/fleet/heartbeat":
        _metrics.inc("flipside_heartbeats_total")
    if _settings.log_json:
        # Only under JSON logging: uvicorn already prints a readable access log,
        # and two access logs in the console is worse than one.
        _logs.log.info("request", extra={
            "method": request.method, "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "actor": getattr(getattr(request.state, "principal", None), "name", ""),
            "ip": request.client.host if request.client else "",
        })
    if request.method != "GET" and request.url.path.startswith("/api") \
            and request.url.path not in _UNAUDITED:
        principal = getattr(request.state, "principal", None)
        _audit.record(
            actor=principal.name if principal else "-",
            role=principal.role if principal else "",
            method=request.method, path=request.url.path,
            status=response.status_code,
            ip=request.client.host if request.client else "",
            summary=getattr(request.state, "audit_summary", ""),
        )
    return response


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": __version__}


# Every router in app.routers is mounted, rather than a hand-written list.
# The list was the bug: the imaging router was imported and then left out of it,
# so /api/imaging fell through to the SPA handler below and answered "Not found"
# -- a plausible-looking 404 from a route that was never registered at all. A
# new router file is now reachable the moment it exists.
for _mod in pkgutil.iter_modules(_routers.__path__):
    _router = getattr(importlib.import_module(f"app.routers.{_mod.name}"), "router", None)
    if _router is not None:
        app.include_router(_router, prefix="/api")

# Prometheus scrapes /metrics by default, and every scrape config, dashboard and
# operator muscle-memory assumes it. Mounted at the root as well as under /api
# so nobody has to discover that this one is somewhere else — without it the SPA
# catch-all below answers /metrics with the front page, which a scraper stores
# as a failed parse rather than reporting as a wrong path.
from app.routers.metrics import prometheus as _prometheus   # noqa: E402
app.add_api_route("/metrics", _prometheus, methods=["GET"], include_in_schema=False)

_assets = os.path.join(STATIC_DIR, "assets")
if os.path.isdir(_assets):
    app.mount("/assets", StaticFiles(directory=_assets), name="assets")


# Paths that belong to the provisioning HTTP server, not to this app. Answering
# them with the SPA is worse than answering nothing: rauc streamed index.html
# from /bundles/x.raucb, read the last eight bytes of the page as the bundle's
# signature size, and reported "Signature size (4336799815442382346) exceeds
# bundle size" -- which is "</html>\n" as a big-endian integer, and reads like a
# corrupt bundle rather than a wrong port.
_NOT_OURS = ("bundles/", "images/", "imager/", "hosts/")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    if full_path.startswith(_NOT_OURS):
        return JSONResponse(status_code=404, content={
            "detail": f"/{full_path} is served by the provisioning server on port 80, "
                      "not by the web UI"})
    # Resolve and contain: os.path.join ignores the base for absolute paths,
    # and encoded ../ sequences arrive decoded — both would escape STATIC_DIR.
    root = os.path.realpath(STATIC_DIR)
    candidate = os.path.realpath(os.path.join(root, full_path))
    if full_path and candidate.startswith(root + os.sep) and os.path.isfile(candidate):
        return FileResponse(candidate)
    # Past that point the file does not exist. A path whose last segment has an
    # extension was asking for a file rather than for a client-side route, so
    # 404 instead of falling through: an HTML body under a name that promises
    # otherwise fails somewhere further along than the mistake, which is how a
    # wrong port turned into a corrupt-bundle report above. Real assets are
    # served by the branch just above and by the /assets mount, so this only
    # ever catches names that are not there.
    if "." in full_path.rsplit("/", 1)[-1]:
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return JSONResponse(content={"message": "Flipside UI API", "version": __version__})
