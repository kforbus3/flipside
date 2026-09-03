"""The control plane: where agents check in, and where operators group machines.

The heartbeat endpoint is the only place in this API that a machine talks to,
and the only place an update is ever handed out. It is a poll, not a push, for
a reason that is a property of the deployment rather than a preference: a
machine is imaged on a private provisioning switch and then moves to wherever it
is going to live. From that moment this server does not know its address, very
likely cannot route to it, and should not want an inbound port open on it. So
the server holds the desired state and the machines come and ask.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from .. import orchestrator as orch
from ..config import settings
from ..deployments import deployments
from ..fleet import fleet
from ..rollouts import rollouts
from ..security import Principal, require_operator, require_viewer

router = APIRouter(tags=["fleet"])

# Fields an agent may set on its own record. An allowlist rather than "whatever
# was posted": this endpoint is unauthenticated by default, and without it any
# machine could write `groups` onto itself and join a rollout it was never meant
# to be in. Group membership is an operator's word about a machine, never the
# machine's word about itself.
_AGENT_FIELDS = ("hostname", "slot", "version", "image", "agent_version", "arch",
                 "uptime", "boot_id", "health", "update_state", "update_error",
                 "update_rollout", "os")


def _clean(value: str, limit: int = 200) -> str:
    """One line, bounded. The response format below is line-oriented and these
    values are echoed into it; a newline in a version string would otherwise let
    a machine inject fields into its own directive."""
    return value.replace("\n", " ").replace("\r", " ").strip()[:limit]


def _kv(pairs: dict[str, object]) -> Response:
    """The agent-facing wire format: `key=value`, one per line.

    Deliberately not JSON. The agent is a shell script on a minimal Debian
    image that ships neither jq nor python3 — adding either to every image, on
    every machine, to parse a handful of fields would be a strange price to pay,
    and hand-rolling a JSON parser in sed to avoid it would be worse. Values are
    single-line and the key is everything before the first `=`, so `read`
    and `case` handle it in four lines with no parser at all.

    The operator-facing endpoints in this file answer JSON as usual.
    """
    body = "".join(f"{k}={v}\n" for k, v in pairs.items() if v not in (None, ""))
    return Response(content=body, media_type="text/plain; charset=utf-8")


@router.post("/fleet/heartbeat")
async def heartbeat(request: Request):
    """One agent check-in. Answers with what, if anything, the machine should do.

    Unauthenticated by default, on the same reasoning as /api/imaging/report and
    /api/imaging/checkin: it is reached by machines this server provisioned and
    handed no credential to. What it accepts is bounded to the allowlist above,
    what it can cause is bounded to "install a bundle this server is already
    offering, signed by a key the machine already trusts" — a machine that lies
    its way into a rollout receives an update it would have been given anyway.
    Set AGENT_TOKEN when the control plane is exposed somewhere less friendly.
    """
    if settings.agent_token:
        presented = request.headers.get("x-flipside-agent-token", "")
        if presented != settings.agent_token:
            raise HTTPException(401, "bad or missing agent token")

    form = dict(await request.form())
    ident = _clean(str(form.get("id") or ""), 128)
    if not ident:
        return _kv({"ok": "false", "error": "id is required"})

    reported = {k: _clean(str(form.get(k) or "")) for k in _AGENT_FIELDS}
    reported["address"] = request.client.host if request.client else ""
    fleet.heartbeat(ident, **reported)

    # Evaluated after the report is recorded, so the rollout decides against
    # what this machine just said rather than against the previous heartbeat.
    action = rollouts.evaluate(ident, reported)

    out: dict[str, object] = {"ok": "true", "interval": settings.agent_interval}
    # Re-point the fleet centrally. An agent adopts this and persists it, which
    # is the way out of the imaging-address trap: the URL the imager wrote is
    # the provisioning server's, and the machine can no longer reach it.
    advertised = orch.control_url()
    if advertised:
        out["control_url"] = advertised
    if action:
        out["action"] = action["type"]
        out["bundle_url"] = action["bundle_url"]
        out["version"] = action["version"]
        out["rollout"] = action["rollout"]
    else:
        out["action"] = "none"
    return _kv(out)


@router.get("/fleet")
async def list_fleet(_: Principal = Depends(require_viewer)):
    """Every machine, live state merged with what it was imaged with."""
    rows = fleet.machines(settings.agent_interval)
    # deployments.jsonl knows what each machine was given and when, which the
    # agent has no way to report — nothing on a running system records which
    # image file it was written from.
    history = {m["id"]: m for m in deployments.fleet()}
    for row in rows:
        past = history.get(row["id"], {})
        row.setdefault("image", past.get("image", ""))
        row["imaged_at"] = past.get("imaged_at")
        row["booted_at"] = past.get("booted_at")
        row["provision_state"] = past.get("state", "")
    # A machine that was imaged but has never run an agent still belongs here.
    for ident, past in history.items():
        if not any(r["id"] == ident for r in rows):
            rows.append({"id": ident, "presence": "unknown", "groups": [],
                         "label": "", "paused": False,
                         "hostname": past.get("hostname", ""),
                         "address": past.get("address", ""),
                         "slot": past.get("slot", ""),
                         "version": past.get("version", ""),
                         "image": past.get("image", ""),
                         "imaged_at": past.get("imaged_at"),
                         "booted_at": past.get("booted_at"),
                         "provision_state": past.get("state", "")})
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["presence"]] = counts.get(row["presence"], 0) + 1
    versions: dict[str, int] = {}
    for row in rows:
        if row.get("version") and row["presence"] in ("online", "stale"):
            versions[row["version"]] = versions.get(row["version"], 0) + 1
    return {"machines": rows, "counts": counts, "versions": versions,
            "interval": settings.agent_interval,
            "control_url": orch.control_url()}


@router.put("/fleet/hosts/{ident}")
async def update_host(ident: str, request: Request, body: dict = Body(...),
                      _: Principal = Depends(require_operator)):
    """Set the operator-owned half of a machine's record: groups, label, hold."""
    fields: dict[str, object] = {}
    if "groups" in body:
        groups = body.get("groups") or []
        if not isinstance(groups, list) or any(not isinstance(g, str) for g in groups):
            raise HTTPException(400, "groups must be a list of names")
        fields["groups"] = sorted({_clean(g, 64) for g in groups if g.strip()})
    if "label" in body:
        fields["label"] = _clean(str(body.get("label") or ""), 128)
    if "paused" in body:
        fields["paused"] = bool(body.get("paused"))
    if not fields:
        raise HTTPException(400, "nothing to change")
    request.state.audit_summary = f"host {ident}: " + ", ".join(
        f"{k}={v}" for k, v in fields.items())
    return fleet.set_host(ident, **fields)


@router.get("/fleet/groups")
async def list_groups(_: Principal = Depends(require_viewer)):
    return {"groups": sorted(fleet.groups().values(), key=lambda g: g["name"])}


@router.put("/fleet/groups/{name}")
async def put_group(name: str, body: dict = Body(default={}),
                    _: Principal = Depends(require_operator)):
    name = _clean(name, 64)
    if not name:
        raise HTTPException(400, "a group needs a name")
    return fleet.set_group(name, _clean(str(body.get("description") or ""), 200))


@router.delete("/fleet/groups/{name}")
async def delete_group(name: str, _: Principal = Depends(require_operator)):
    """Delete a group. Machines in it are not touched beyond losing it.

    Refused while a running rollout targets it, because the rollout's target is
    resolved live: deleting the group would silently empty it, and an emptied
    rollout reads as "finished" rather than as "you deleted the thing it was
    for".
    """
    for rec in rollouts.list():
        if rec["state"] in ("running", "paused") and name in rec["target"]["groups"]:
            raise HTTPException(409, f"rollout {rec['id']} is targeting this group; "
                                     "cancel it first")
    return {"hosts_updated": fleet.delete_group(name)}


@router.get("/fleet/enrollment")
async def enrollment(_: Principal = Depends(require_viewer)):
    """What a machine needs to be told to reach this control plane.

    Surfaced because the answer is not guessable and getting it wrong is the
    quietest failure in the system: the machine keeps running perfectly and
    simply never appears.
    """
    return {"control_url": orch.control_url(),
            "interval": settings.agent_interval,
            "token_required": bool(settings.agent_token),
            "command": ("ab-agent --set-server "
                        f"{orch.control_url() or 'https://flipside.example.com'}"),
            "now": time.time()}
