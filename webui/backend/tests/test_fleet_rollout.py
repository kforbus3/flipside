#!/usr/bin/env python3
"""The control plane, driven the way a real fleet drives it: one heartbeat at a time.

Every failure this file guards against is silent in production, which is why it
exists rather than a couple of endpoint smoke tests. A rollout that counts an
install as a success ships a bricking update to the whole fleet while every
machine quietly rolls back. A rollout that never advances past the canary leaves
an operator watching a progress bar that will not move. A rollout that ignores
its failure budget does the first thing on purpose. None of those raise, none of
them log an error, and all of them look fine from the outside until the fleet is
already on the bad version.

The machines here are simulated by posting to the real endpoint, because the
engine's whole job is to decide from what a machine says -- and a test that
called the store directly would prove nothing about the endpoint an agent
actually talks to.
"""
import os
import sys
import tempfile
import time

PROJ = tempfile.mkdtemp()
os.makedirs(os.path.join(PROJ, "output", "bundles"), exist_ok=True)
os.environ.update(PROJECT_DIR=PROJ, STATIC_DIR="/tmp/none",
                  ADMIN_PASSWORD="ci-pw", SECRET_KEY="ci-secret",
                  CONTROL_URL="http://flipside.example.com")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fastapi.testclient import TestClient   # noqa: E402
from app.main import app                    # noqa: E402
from app import rollouts as rollouts_mod    # noqa: E402
from app.fleet import fleet, presence       # noqa: E402

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


TOKEN = client.post("/api/auth/login",
                    data={"username": "admin", "password": "ci-pw"}).json()["access_token"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# A bundle to roll out. The sidecar is what the API reads for the version, and
# the version is what a machine is checked against, so a bundle without one is
# refused further down.
BUNDLES = os.path.join(PROJ, "output", "bundles")
open(os.path.join(BUNDLES, "flipside-2.0.raucb"), "wb").write(b"hsqs-not-really")
with open(os.path.join(BUNDLES, "flipside-2.0.raucb.json"), "w") as f:
    f.write('{"version":"2.0","compatible":"flipside","source":"x.img"}')
open(os.path.join(BUNDLES, "unversioned.raucb"), "wb").write(b"hsqs")
with open(os.path.join(BUNDLES, "unversioned.raucb.json"), "w") as f:
    f.write('{"compatible":"flipside"}')


def beat(ident, version="1.0", state="idle", health="ok", error=""):
    """One agent check-in. Returns the parsed key=value directive."""
    r = client.post("/api/fleet/heartbeat", data={
        "id": ident, "hostname": ident, "version": version, "slot": "A",
        "health": health, "update_state": state, "update_error": error,
        "boot_id": f"{ident}-boot"})
    out = {}
    for line in r.text.splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def rollout(rid):
    return client.get(f"/api/rollouts/{rid}", headers=AUTH).json()


print("== a machine appears by checking in, and is told nothing to do ==")
d = beat("m1")
check("heartbeat is accepted unauthenticated", d.get("ok") == "true", d)
check("with nothing to do", d.get("action") == "none", d)
check("and is told where to report from now on",
      d.get("control_url") == "http://flipside.example.com", d)
rows = client.get("/api/fleet", headers=AUTH).json()
check("the machine is in the fleet", any(m["id"] == "m1" for m in rows["machines"]))
check("and reads as online", rows["machines"][0]["presence"] == "online",
      rows["machines"][0].get("presence"))

print("== presence is about silence, not about failure ==")
now = time.time()
check("a machine heard from just now is online", presence(now, 300, now) == "online")
check("one missed beat is still online", presence(now - 400, 300, now) == "online")
check("four missed beats is stale", presence(now - 1300, 300, now) == "stale")
check("a day of silence is offline", presence(now - 90000, 300, now) == "offline")
# The distinction that stops the page filling with false alarms: a machine that
# never ran an agent has not gone quiet, it was never speaking.
check("never heard from at all is unknown, not offline", presence(None, 300, now) == "unknown")

print("== groups are the operator's word, never the machine's ==")
for host in ("m1", "m2", "m3", "m4", "m5"):
    beat(host)
    client.put(f"/api/fleet/hosts/{host}", json={"groups": ["prod"]}, headers=AUTH)
# An agent posting `groups` must not be able to put itself in one -- otherwise
# any machine on the network can opt into a rollout it was never targeted by.
client.post("/api/fleet/heartbeat", data={"id": "intruder", "groups": "prod"})
members = fleet.members(["prod"], [], False)
check("the five real machines are in prod", len(members) == 5, members)
check("a machine cannot add itself to a group", "intruder" not in members, members)

print("== a rollout with no version to check against is refused up front ==")
r = client.post("/api/rollouts", json={"bundle": "unversioned.raucb", "groups": ["prod"]},
                headers=AUTH)
check("an unversioned bundle cannot be rolled out", r.status_code == 400, r.status_code)

print("== canary first: exactly one machine is offered the update ==")
r = client.post("/api/rollouts", json={
    "bundle": "flipside-2.0.raucb", "groups": ["prod"],
    "strategy": {"canary": 1, "batch_size": 10, "soak_seconds": 0, "max_failures": 2},
}, headers=AUTH)
check("the rollout is created", r.status_code == 200, r.text[:200])
RID = r.json()["id"]
check("it targets all five", r.json()["total"] == 5, r.json().get("total"))
check("and the URL machines are given is the reachable one",
      r.json()["bundle_url"] == "http://flipside.example.com/bundles/flipside-2.0.raucb",
      r.json().get("bundle_url"))

offered = [h for h in ("m1", "m2", "m3", "m4", "m5") if beat(h).get("action") == "update"]
check("exactly one machine is offered the canary", len(offered) == 1, offered)
CANARY = offered[0]
others = [h for h in ("m1", "m2", "m3", "m4", "m5") if h != CANARY]

print("== an install that says it worked is NOT a success ==")
beat(CANARY, state="installing")
check("the canary reads as installing",
      rollout(RID)["machines"][CANARY]["state"] == "installing")
beat(CANARY, state="installed")
check("and then as rebooting, not verified",
      rollout(RID)["machines"][CANARY]["state"] == "rebooting")
# This is the one that matters. The machine claims the install finished, but it
# is still running the old version -- which is precisely what a bundle that
# installs and then fails to boot looks like. Counting this as success is how a
# bricking update reaches an entire fleet.
for host in others:
    beat(host)
check("no second machine starts on the strength of an install alone",
      sum(1 for h in others if rollout(RID)["machines"][h]["state"] != "pending") == 0,
      rollout(RID)["counts"])

print("== success is the machine coming back, on the new version, healthy ==")
beat(CANARY, version="2.0", state="installed")
check("the canary is verified once it reports the target version",
      rollout(RID)["machines"][CANARY]["state"] == "verified")

print("== a machine that comes back degraded is not a success either ==")
# Roll the clock back on the verified canary so the batch can proceed, then
# check that health is actually consulted rather than assumed.
second = others[0]
d = beat(second)
check("the next machine is now offered the update", d.get("action") == "update", d)
beat(second, version="2.0", state="installed", health="degraded")
check("a degraded machine on the target version is not verified",
      rollout(RID)["machines"][second]["state"] != "verified",
      rollout(RID)["machines"][second])
beat(second, version="2.0", state="installed", health="ok")
check("and is verified once it comes up clean",
      rollout(RID)["machines"][second]["state"] == "verified")

print("== the failure budget stops the rollout on its own ==")
for host in others[1:3]:
    beat(host)                                        # take the offer
    beat(host, state="failed", error="rauc exit 1")   # and report failure
rec = rollout(RID)
check("two failures halt the rollout", rec["state"] == "halted", rec["state"])
check("and it says why", "failed" in rec["halt_reason"], rec["halt_reason"])
d = beat(others[3])
check("a halted rollout offers nothing further", d.get("action") == "none", d)

print("== resuming is deliberate, and picks up where it stopped ==")
r = client.post(f"/api/rollouts/{RID}/resume", headers=AUTH)
check("a halted rollout can be resumed", r.status_code == 200, r.text[:200])
d = beat(others[3])
check("the remaining machine is offered the update again", d.get("action") == "update", d)

print("== the soak keeps the fleet behind the canary ==")
fleet.reload()
rollouts_mod.rollouts.reload()
for host in ("s1", "s2", "s3"):
    beat(host)
    client.put(f"/api/fleet/hosts/{host}", json={"groups": ["soak"]}, headers=AUTH)
r = client.post("/api/rollouts", json={
    "bundle": "flipside-2.0.raucb", "groups": ["soak"],
    "strategy": {"canary": 1, "batch_size": 10, "soak_seconds": 3600, "max_failures": 5},
}, headers=AUTH)
SID = r.json()["id"]
canary = next(h for h in ("s1", "s2", "s3") if beat(h).get("action") == "update")
beat(canary, version="2.0", state="installed")
check("the soak canary verifies", rollout(SID)["machines"][canary]["state"] == "verified")
rest = [h for h in ("s1", "s2", "s3") if h != canary]
check("but nothing else starts during the soak",
      all(beat(h).get("action") == "none" for h in rest),
      rollout(SID)["counts"])
# Reach in and age the canary past the soak. The alternative is a test that
# sleeps for an hour, and a soak nobody can test is a soak nobody trusts.
rec = rollouts_mod.rollouts.get(SID)
rec["machines"][canary]["at"] -= 7200
check("once the soak has elapsed the batch proceeds",
      all(beat(h).get("action") == "update" for h in rest),
      rollout(SID)["counts"])

print("== a held machine is skipped without holding up the rollout ==")
fleet.reload()
rollouts_mod.rollouts.reload()
for host in ("h1", "h2"):
    beat(host)
    client.put(f"/api/fleet/hosts/{host}", json={"groups": ["held"]}, headers=AUTH)
client.put("/api/fleet/hosts/h1", json={"paused": True}, headers=AUTH)
r = client.post("/api/rollouts", json={
    "bundle": "flipside-2.0.raucb", "groups": ["held"],
    "strategy": {"canary": 0, "batch_size": 10, "soak_seconds": 0, "max_failures": 5},
}, headers=AUTH)
HID = r.json()["id"]
check("a held machine is offered nothing", beat("h1").get("action") == "none")
check("while the rest of the group proceeds", beat("h2").get("action") == "update")

print("== a maintenance window is enforced by the server, not the machine ==")
fleet.reload()
rollouts_mod.rollouts.reload()
beat("w1")
client.put("/api/fleet/hosts/w1", json={"groups": ["win"]}, headers=AUTH)
# A window that cannot contain the present moment, whenever the suite runs.
r = client.post("/api/rollouts", json={
    "bundle": "flipside-2.0.raucb", "groups": ["win"],
    "strategy": {"canary": 0, "batch_size": 5, "soak_seconds": 0, "max_failures": 5},
    "window": {"start": "03:00", "end": "03:01", "days": []},
}, headers=AUTH)
WID = r.json()["id"]
check("outside its window a rollout offers nothing", beat("w1").get("action") == "none")
rec = rollouts_mod.rollouts.get(WID)
rec["window"] = {"start": "00:00", "end": "23:59", "days": list(range(7))}
check("inside its window it does", beat("w1").get("action") == "update")

print("== deleting a group out from under a live rollout is refused ==")
r = client.delete("/api/fleet/groups/win", headers=AUTH)
check("the group cannot be deleted while targeted", r.status_code == 409, r.status_code)
client.post(f"/api/rollouts/{WID}/cancel", headers=AUTH)
r = client.delete("/api/fleet/groups/win", headers=AUTH)
check("and can once the rollout is cancelled", r.status_code == 200, r.status_code)

print("== an abandoned offer goes back in the pool rather than wedging ==")
fleet.reload()
rollouts_mod.rollouts.reload()
for host in ("t1", "t2"):
    beat(host)
    client.put(f"/api/fleet/hosts/{host}", json={"groups": ["timeout"]}, headers=AUTH)
r = client.post("/api/rollouts", json={
    "bundle": "flipside-2.0.raucb", "groups": ["timeout"],
    "strategy": {"canary": 1, "batch_size": 1, "soak_seconds": 0, "max_failures": 9},
}, headers=AUTH)
TID = r.json()["id"]
taken = next(h for h in ("t1", "t2") if beat(h).get("action") == "update")
other = "t2" if taken == "t1" else "t1"
check("the other machine waits its turn", beat(other).get("action") == "none")
# The machine that took the offer has gone silent -- powered off mid-download,
# say. Without a timeout the single canary slot is occupied forever and the
# rollout never finishes, with nothing anywhere saying why.
rec = rollouts_mod.rollouts.get(TID)
rec["machines"][taken]["at"] -= rollouts_mod.OFFER_TIMEOUT_SECONDS + 60
check("once the offer times out the slot is freed",
      beat(other).get("action") == "update", rollout(TID)["counts"])

print("== the audit trail covers the control plane, but not the machines ==")
lines = client.get("/api/audit", headers=AUTH).json()["events"]
paths = [e["path"] for e in lines]
check("creating a rollout is audited", "/api/rollouts" in paths, paths[:5])
check("heartbeats are not", "/api/fleet/heartbeat" not in paths,
      [p for p in paths if "heartbeat" in p][:3])

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
