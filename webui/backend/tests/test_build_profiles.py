#!/usr/bin/env python3
"""Build profiles: the right arguments reach the builder, and the wrong
combinations never reach a job.

Run it directly -- no pytest, no network:

    cd webui/backend && python tests/test_build_profiles.py

Three failures this prevents, in the order they would be discovered:

  a build request that never mentions a profile must produce exactly the
  command it always has -- one stray flag here and every existing caller's
  images change underneath them;

  a desktop environment the distro does not package (cinnamon on Ubuntu) must
  be a 400 naming what IS available, before any job starts -- the builder
  would refuse it too, but only after the operator has walked away from a
  twenty-minute build;

  and the desktop size floor must reach the image-size check, or an
  image_size that fits a minimal build sails through validation and dies as
  "No space left on device" deep inside dpkg.
"""
import os
import sys
import tempfile

PROJ = tempfile.mkdtemp()
os.makedirs(os.path.join(PROJ, "output"), exist_ok=True)
os.environ.update(PROJECT_DIR=PROJ, STATIC_DIR="/tmp/none",
                  ADMIN_PASSWORD="ci", SECRET_KEY="ci-secret-key")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import orchestrator as orch  # noqa: E402
from app.routers.builds import _validate_build  # noqa: E402

client = TestClient(app)
tok = client.post("/api/auth/login", data={"username": "admin", "password": "ci"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  PASS  {name}")
    else:
        fail += 1; print(f"  FAIL  {name} {extra}")


def script_for(opts):
    cmd, _label, _env = orch.build_image_cmd(dict(opts))
    return cmd[-1]


def refused(opts):
    """The HTTPException _validate_build raises, or None if it accepted."""
    try:
        _validate_build(dict(opts))
        return None
    except HTTPException as exc:
        return exc


base = {"distro": "debian", "suite": "trixie", "arch": "amd64", "password": "x"}

print("== the default build command is unchanged ==")
# No profile key at all, and profile=minimal spelled out, must both produce the
# command this UI has always produced -- byte for byte.
plain = script_for(base)
check("no --profile in the default command", "--profile" not in plain)
check("no --desktop in the default command", "--desktop" not in plain)
check("profile=minimal is the same command", script_for({**base, "profile": "minimal"}) == plain)
check("empty profile is the same command", script_for({**base, "profile": ""}) == plain)

print("== valid combinations put the right args in the command ==")
s = script_for({**base, "profile": "server"})
check("server adds --profile server", "'--profile' 'server'" in s, s[-200:])
check("server adds no --desktop", "--desktop" not in s)
s = script_for({**base, "profile": "desktop", "desktop": "kde"})
check("desktop/kde adds both flags", "'--profile' 'desktop' '--desktop' 'kde'" in s, s[-200:])
s = script_for({**base, "profile": "desktop"})
check("desktop with no environment leaves the default to the builder (gnome)",
      "'--profile' 'desktop'" in s and "--desktop" not in s, s[-200:])
s = script_for({**base, "distro": "ubuntu", "suite": "noble",
                "profile": "desktop", "desktop": "xfce"})
check("ubuntu desktop/xfce passes through", "'--desktop' 'xfce'" in s, s[-200:])

print("== every advertised combination validates ==")
for distro, envs in orch.DESKTOP_ENVS.items():
    for env in envs:
        exc = refused({**base, "distro": distro, "profile": "desktop", "desktop": env})
        check(f"{distro}/{env} is accepted", exc is None, getattr(exc, "detail", ""))

print("== impossible combinations are refused with the alternatives ==")
exc = refused({**base, "distro": "ubuntu", "profile": "desktop", "desktop": "cinnamon"})
check("cinnamon on ubuntu is a 400", exc is not None and exc.status_code == 400,
      exc and exc.status_code)
check("...whose message lists what ubuntu has",
      exc is not None and "xfce" in str(exc.detail) and "lxqt" in str(exc.detail),
      exc and exc.detail)
exc = refused({**base, "profile": "desktop", "desktop": "unity"})
check("an unknown environment is a 400", exc is not None and exc.status_code == 400)
check("...listing debian's set", exc is not None and "cinnamon" in str(exc.detail),
      exc and exc.detail)
exc = refused({**base, "profile": "workstation"})
check("an unknown profile is a 400", exc is not None and exc.status_code == 400)
check("...naming the real profiles", exc is not None and "server" in str(exc.detail),
      exc and exc.detail)
exc = refused({**base, "profile": "server", "desktop": "gnome"})
check("desktop without profile=desktop is a 400",
      exc is not None and exc.status_code == 400, exc and getattr(exc, "detail", ""))
exc = refused({**base, "desktop": "gnome"})
check("desktop with no profile at all is a 400",
      exc is not None and exc.status_code == 400)

print("== the desktop size floor reaches the image-size check ==")
# 22 GiB comfortably holds two 10240 MiB desktop slots; 8 GiB holds a minimal
# build and not a desktop one. If the floor stopped being applied, the second
# case would validate and the failure would move into dpkg, an hour in.
check("a minimal build at 8 GiB validates",
      refused({**base, "image_size": 8}) is None)
exc = refused({**base, "profile": "desktop", "image_size": 8})
check("a desktop build at 8 GiB is refused up front",
      exc is not None and exc.status_code == 400, exc and getattr(exc, "detail", ""))
check("a desktop build at 22 GiB validates",
      refused({**base, "profile": "desktop", "image_size": 22}) is None)

print("== Secure Boot reaches the builder, and only when it is not the default ==")
# auto is the builder's own default, so a build request that never heard of
# Secure Boot has to produce exactly the command it always did -- otherwise
# every existing caller silently changes what it builds.
cmd, _label, _env = orch.build_image_cmd({**base})
check("a request with no secure_boot passes no flag",
      "--secure-boot" not in " ".join(cmd), cmd)
cmd, _l, _e = orch.build_image_cmd({**base, "secure_boot": "auto"})
check("nor does an explicit auto", "--secure-boot" not in " ".join(cmd), cmd)
for mode in ("on", "off"):
    cmd, _l, _e = orch.build_image_cmd({**base, "secure_boot": mode})
    # The arguments are shell-quoted into the generated script, so match the
    # quoted pair rather than the bare words -- checking for "--secure-boot on"
    # would fail on a command that is perfectly correct.
    check(f"secure_boot={mode} reaches the builder",
          f"'--secure-boot' '{mode}'" in " ".join(cmd), cmd[-1][-200:])
exc = refused({**base, "secure_boot": "yes-please"})
check("an unknown mode is refused up front",
      exc is not None and exc.status_code == 400, exc and getattr(exc, "detail", ""))

print("== the API refuses before any job exists ==")
# Through the real route: authentication, preflight, validation, in that
# order. preflight is stubbed -- this container has no Docker socket, and a
# 503 for that would pass a status-code check while proving nothing about
# validation.
real_preflight = orch.preflight
orch.preflight = lambda: []
try:
    r = client.post("/api/builds", headers=H, json={
        **base, "distro": "ubuntu", "suite": "noble",
        "profile": "desktop", "desktop": "cinnamon",
    })
    check("the route answers 400", r.status_code == 400, r.status_code)
    check("...with the available environments", "xfce" in r.text, r.text[:200])
    jobs = client.get("/api/jobs", headers=H).json()
    check("no job was started", jobs == [], jobs)
finally:
    orch.preflight = real_preflight

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
