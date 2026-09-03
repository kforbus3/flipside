"""The Prometheus scrape endpoint, and a JSON view of the same numbers.

Behind the ordinary viewer role by default, so a scrape authenticates with an
API token like anything else. That is a deliberate default rather than caution
for its own sake: this endpoint names the versions running in the field, how
many machines there are, which rollouts are live and how they are going. It is
a fair map of the estate, and an unauthenticated one is a map anyone on the
network can take a copy of.

METRICS_PUBLIC=true removes that, for the common case of a scrape target on a
network where it does not matter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from app import metrics as m
from app.config import settings
from app.security import Principal, require_viewer

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def prometheus(request: Request):
    if not settings.metrics_public:
        # Depends() would have been resolved before the handler ran, which is
        # exactly what must not happen while metrics_public is true.
        from app.security import _resolve, oauth2_scheme
        token = await oauth2_scheme(request)
        request.state.principal = _resolve(token or "")
    body = await run_in_threadpool(m.render)
    # The 0.0.4 content type is what Prometheus expects; without it some
    # scrapers store the body as a string and silently record nothing.
    return Response(content=body,
                    media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/metrics.json")
async def metrics_json(_: Principal = Depends(require_viewer)):
    """The same numbers as an object, for the dashboard and for anyone who does
    not run Prometheus. Parsing the exposition format to draw one tile would be
    a strange thing to make a browser do."""
    from app.deployments import deployments
    from app.fleet import fleet
    from app.forwarder import forwarder, stats as fwd
    from app.rollouts import rollouts

    machines = fleet.machines(settings.agent_interval)
    presence: dict[str, int] = {"online": 0, "stale": 0, "offline": 0, "unknown": 0}
    for row in machines:
        presence[row["presence"]] = presence.get(row["presence"], 0) + 1
    live = [r for r in rollouts.list() if r["state"] in ("running", "paused", "halted")]
    return {
        "fleet": {
            "machines": len(machines),
            "presence": presence,
            "degraded": sum(1 for r in machines if r.get("health") == "degraded"),
            "never_booted": sum(1 for r in deployments.fleet()
                                if r.get("state") == "never-booted"),
        },
        "rollouts": {
            "live": len(live),
            "halted": sum(1 for r in live if r["state"] == "halted"),
        },
        "audit_forwarding": {
            "enabled": forwarder.enabled(),
            "sent": fwd.sent, "failed": fwd.failed,
            "dropped": fwd.dropped, "queued": fwd.queued,
            "last_error": fwd.last_error,
            "last_success": fwd.last_success,
        },
    }
