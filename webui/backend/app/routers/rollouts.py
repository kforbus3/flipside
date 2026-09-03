"""Create and steer staged rollouts.

Nothing here talks to a machine. Creating a rollout writes down an intention;
machines discover it on their next heartbeat (see routers/fleet.py). That means
"start a rollout" returns instantly and cannot fail because a machine was
asleep, and "pause a rollout" takes effect for every machine that has not
already been handed a bundle — without a single connection being made.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .. import orchestrator as orch
from ..fleet import fleet
from ..rollouts import rollouts
from ..security import Principal, require_operator, require_viewer

router = APIRouter(tags=["rollouts"])


@router.get("/rollouts")
async def list_rollouts(_: Principal = Depends(require_viewer)):
    return {"rollouts": rollouts.list()}


@router.get("/rollouts/{rid}")
async def get_rollout(rid: str, _: Principal = Depends(require_viewer)):
    if rollouts.get(rid) is None:
        raise HTTPException(404, "no such rollout")
    return rollouts.public(rid)


@router.post("/rollouts")
async def create_rollout(request: Request, body: dict = Body(...),
                         principal: Principal = Depends(require_operator)):
    """Target a bundle at a group, a list of machines, or the whole fleet."""
    bundle = str(body.get("bundle") or "").strip()
    if not bundle:
        raise HTTPException(400, "bundle is required")
    if "/" in bundle or ".." in bundle:
        raise HTTPException(400, "bundle must be a filename in the bundle library")

    known = {b["name"]: b for b in await run_in_threadpool(orch.list_bundles)}
    if bundle not in known:
        raise HTTPException(404, f"no such bundle: {bundle}")
    version = str(known[bundle].get("version") or "").strip()
    if not version:
        # Without a version there is no way to tell a machine that installed the
        # bundle from one that did not, so the rollout could never finish. Better
        # to refuse at creation than to leave one running forever.
        raise HTTPException(400, f"{bundle} has no version recorded in its sidecar; "
                                 "rebuild it so machines can be checked against it")

    groups = [g for g in (body.get("groups") or []) if isinstance(g, str)]
    hosts = [h for h in (body.get("hosts") or []) if isinstance(h, str)]
    everything = bool(body.get("all"))
    if not (groups or hosts or everything):
        raise HTTPException(400, "a rollout needs a target: groups, hosts, or all")

    members = fleet.members(groups, hosts, everything)
    if not members:
        raise HTTPException(400, "that target matches no machines")

    # The URL machines will fetch from. It is deliberately not derived from the
    # request: this request came from an operator's browser, and the address
    # that reaches the UI is routinely not the address a machine out in the
    # field can reach — which is the whole reason UPDATE_IP exists.
    base = str(body.get("bundle_base") or orch.control_url() or "").rstrip("/")
    if not base:
        raise HTTPException(400, "set CONTROL_URL (or pass bundle_base) so machines "
                                 "are given a URL they can actually reach; the address "
                                 "you are using to read this is the provisioning "
                                 "server's, which a deployed machine usually is not on")
    bundle_url = f"{base}/bundles/{bundle}"

    strategy = body.get("strategy") or {}
    try:
        canary = max(0, int(strategy.get("canary", 1)))
        batch_size = max(1, int(strategy.get("batch_size", 10)))
        soak = max(0, int(strategy.get("soak_seconds", 900)))
        max_failures = max(0, int(strategy.get("max_failures", 2)))
    except (TypeError, ValueError):
        raise HTTPException(400, "strategy values must be whole numbers")

    window = body.get("window") or None
    if window is not None and not isinstance(window, dict):
        raise HTTPException(400, "window must be an object")

    rec = rollouts.create(
        bundle=bundle, version=version, bundle_url=bundle_url,
        groups=groups, hosts=hosts, everything=everything,
        canary=canary, batch_size=batch_size, soak_seconds=soak,
        max_failures=max_failures, window=window,
        created_by=principal.name,
        description=str(body.get("description") or ""))
    request.state.audit_summary = (f"rollout {rec['id']}: {bundle} -> "
                                   f"{len(members)} machines")
    return rec


@router.post("/rollouts/{rid}/{verb}")
async def steer(rid: str, verb: str, request: Request,
                _: Principal = Depends(require_operator)):
    """pause | resume | cancel.

    Pausing stops further machines being offered the bundle; it does not recall
    it from a machine already installing, because there is no way to reach one
    and interrupting an install is how a machine ends up on neither version.
    """
    rec = rollouts.get(rid)
    if rec is None:
        raise HTTPException(404, "no such rollout")
    target = {"pause": "paused", "resume": "running", "cancel": "cancelled"}.get(verb)
    if target is None:
        raise HTTPException(400, "verb must be pause, resume or cancel")
    if verb == "resume" and rec["state"] not in ("paused", "halted"):
        raise HTTPException(409, f"rollout is {rec['state']}, not paused")
    request.state.audit_summary = f"rollout {rid} {verb}d"
    return rollouts.set_state(rid, target)


@router.delete("/rollouts/{rid}")
async def delete_rollout(rid: str, _: Principal = Depends(require_operator)):
    rec = rollouts.get(rid)
    if rec is None:
        raise HTTPException(404, "no such rollout")
    if rec["state"] == "running":
        raise HTTPException(409, "cancel the rollout before deleting it")
    return {"ok": rollouts.delete(rid)}
