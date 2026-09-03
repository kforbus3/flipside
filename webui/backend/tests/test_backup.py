#!/usr/bin/env python3
"""Backup and restore, including the three ways each fails quietly.

A backup has one failure mode and it is silent: it stops containing something,
and nobody finds out until a restore. So the contents are asserted explicitly,
and the shell script's list is checked against the module's — two lists that
have to agree, kept apart by the fact that one runs when the web UI cannot.

A restore has two. It can half-apply, leaving a server holding some of one
state and some of another, which is worse than either and is discovered by
using it. And it can be handed a hostile archive: an admin restoring a file
somebody sent them is a plausible thing to happen, and `../../etc/shadow` as a
tar member name is the oldest trick there is.
"""
import io
import json
import os
import re
import sys
import tarfile
import tempfile

PROJ = tempfile.mkdtemp()
OUT = os.path.join(PROJ, "output")
os.makedirs(os.path.join(OUT, "rauc-keys"), exist_ok=True)
os.makedirs(os.path.join(OUT, "hosts"), exist_ok=True)
os.makedirs(os.path.join(OUT, "fleet"), exist_ok=True)
os.environ.update(PROJECT_DIR=PROJ, STATIC_DIR="/tmp/none",
                  ADMIN_PASSWORD="ci-pw", SECRET_KEY="ci-secret")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from fastapi.testclient import TestClient    # noqa: E402
from app.main import app                     # noqa: E402
from app import backup as backup_mod         # noqa: E402

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


def write(rel, content, mode=0o600):
    full = os.path.join(PROJ, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    os.chmod(full, mode)


TOKEN = client.post("/api/auth/login",
                    data={"username": "admin", "password": "ci-pw"}).json()["access_token"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# The state a real server would have.
write("output/rauc-keys/key.pem", "-----BEGIN PRIVATE KEY-----\nsigning\n")
write("output/rauc-keys/cert.pem", "-----BEGIN CERTIFICATE-----\ncert\n", 0o644)
write("output/hosts/assignments.json", '[{"mac":"aa:bb","image":"x.img"}]')
write("output/deployments.jsonl", '{"event":"imaged","id":"aa:bb"}\n')
write("output/.secrets-store.json", '{"address":"https://bao","token":"s3cret"}')
write("server/.env", "SERVER_IP=192.168.50.1\nCONTROL_URL=https://f.example.com\n")
# Group membership, which exists nowhere else and is somebody's deliberate work.
client.post("/api/fleet/heartbeat", data={"id": "aa:bb", "version": "1.0"})
client.put("/api/fleet/hosts/aa:bb", json={"groups": ["prod"], "label": "web01"},
           headers=AUTH)
backup_mod.reload_stores()

print("== a backup contains everything that cannot be rebuilt ==")
r = client.get("/api/backup", headers=AUTH)
check("an admin can take one", r.status_code == 200, r.status_code)
check("as a gzip attachment",
      r.headers["content-type"] == "application/gzip"
      and "attachment" in r.headers.get("content-disposition", ""), r.headers)
check("that nothing is told to cache",
      r.headers.get("cache-control") == "no-store", r.headers.get("cache-control"))

archive = r.content
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
    names = set(tar.getnames())
    manifest = json.load(tar.extractfile("flipside-backup.json"))

for expected in ("output/rauc-keys/key.pem", "output/users.json",
                 "output/hosts/assignments.json", "output/deployments.jsonl",
                 "output/.secrets-store.json", "server/.env"):
    check(f"it contains {expected}", expected in names, sorted(names))
# The one that is easiest to forget and hardest to reconstruct: group membership
# is not derivable from anything else on the server.
check("it contains the fleet's group membership",
      any(n.startswith("output/fleet/") for n in names), sorted(names))

print("== images are deliberately left out ==")
write("output/debian-trixie-ab.img", "x" * 1000)
data, _ = backup_mod.create()
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
    check("a disk image is not in the backup",
          not any(n.endswith(".img") for n in tar.getnames()), tar.getnames())

print("== nothing in the archive is readable by anyone but its owner ==")
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
    modes = {m.name: m.mode for m in tar.getmembers()}
check("every member is 0600, including the ones that were 0644 on disk",
      all(m == 0o600 for m in modes.values()), modes)
# cert.pem is 0644 on disk and must come back that way; the archive being 0600
# is about a tar extracted by hand somewhere else, not about the restore.
check("but the real mode is recorded so a restore can put it back",
      manifest["files"]["output/rauc-keys/cert.pem"]["mode"] == "0o644",
      manifest["files"]["output/rauc-keys/cert.pem"])

print("== the two contents lists agree ==")
# The shell script exists precisely for when the API cannot run, so it cannot
# import the module's list. A path added to one and forgotten in the other
# produces a backup that silently omits it.
script = open(os.path.join(HERE, "..", "..", "..", "scripts",
                           "flipside-backup.sh")).read()
block = re.search(r"^PATHS=\(\n(.*?)^\)", script, re.S | re.M)
check("the script declares a PATHS list", block is not None)
if block:
    in_script = {line.strip() for line in block.group(1).splitlines() if line.strip()}
    in_module = {rel for rel, _ in backup_mod.CONTENTS}
    check("the script and the module back up the same paths",
          in_script == in_module, in_module ^ in_script)

print("== a restore puts it all back, and only after checking all of it ==")
os.remove(os.path.join(OUT, "rauc-keys", "key.pem"))
os.remove(os.path.join(OUT, "hosts", "assignments.json"))
r = client.post("/api/backup/restore", headers=AUTH,
                files={"file": ("b.tar.gz", archive, "application/gzip")})
check("the restore succeeds", r.status_code == 200, r.text[:200])
check("the signing key is back",
      open(os.path.join(OUT, "rauc-keys", "key.pem")).read().strip().endswith("signing"))
check("so are the assignments", os.path.isfile(os.path.join(OUT, "hosts", "assignments.json")))
check("recorded modes are reapplied rather than left at 0600",
      oct(os.stat(os.path.join(OUT, "rauc-keys", "cert.pem")).st_mode & 0o777) == "0o644",
      oct(os.stat(os.path.join(OUT, "rauc-keys", "cert.pem")).st_mode & 0o777))
check("and the previous state was saved first", r.json().get("safety_copy"), r.json())

print("== group membership survives, and without a restart ==")
# The caches are the trap: several stores read their file once and keep it, so
# a restore that fixes the disk and not the cache reads as one that did nothing.
rows = client.get("/api/fleet", headers=AUTH).json()["machines"]
row = next((m for m in rows if m["id"] == "aa:bb"), {})
check("the restored fleet is live immediately", row.get("groups") == ["prod"], row)

print("== a damaged archive changes nothing ==")
broken = bytearray(archive)
# Corrupt a byte inside the gzip stream rather than the header, so it fails on
# the content rather than being rejected as not-an-archive.
broken[len(broken) // 2] ^= 0xFF
r = client.post("/api/backup/restore", headers=AUTH,
                files={"file": ("b.tar.gz", bytes(broken), "application/gzip")})
check("a corrupt archive is refused", r.status_code == 400, r.status_code)
check("and the good state is still there",
      open(os.path.join(OUT, "rauc-keys", "key.pem")).read().strip().endswith("signing"))

print("== an archive that lies about its contents is refused ==")
# A member whose bytes do not match the checksum the manifest records. Restoring
# it would put quietly-wrong content on disk, which is worse than a failure.
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tar:
    m = {"flipside_backup": 1, "version": "x", "created": "now",
         "files": {"output/users.json": {"size": 4, "sha256": "0" * 64, "mode": "0o600"}}}
    for name, payload in (("output/users.json", b"junk"),
                          ("flipside-backup.json", json.dumps(m).encode())):
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
before = open(os.path.join(OUT, "users.json")).read()
r = client.post("/api/backup/restore", headers=AUTH,
                files={"file": ("b.tar.gz", buf.getvalue(), "application/gzip")})
check("a checksum mismatch is refused", r.status_code == 400, r.status_code)
check("and nothing was written", open(os.path.join(OUT, "users.json")).read() == before)

print("== a hostile archive cannot write outside the project ==")
for hostile in ("../../etc/shadow", "/etc/shadow", "output/../../escape"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = b"pwned"
        digest = __import__("hashlib").sha256(payload).hexdigest()
        m = {"flipside_backup": 1, "version": "x", "created": "now",
             "files": {hostile: {"size": len(payload), "sha256": digest, "mode": "0o600"}}}
        for name, body in ((hostile, payload),
                           ("flipside-backup.json", json.dumps(m).encode())):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    r = client.post("/api/backup/restore", headers=AUTH,
                    files={"file": ("b.tar.gz", buf.getvalue(), "application/gzip")})
    check(f"'{hostile}' is refused", r.status_code == 400, r.status_code)

print("== not a backup at all, said plainly ==")
r = client.post("/api/backup/restore", headers=AUTH,
                files={"file": ("b.tar.gz", b"this is not a tar", "application/gzip")})
check("a file that is not an archive is refused", r.status_code == 400, r.status_code)
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tar:
    info = tarfile.TarInfo("hello.txt")
    info.size = 2
    tar.addfile(info, io.BytesIO(b"hi"))
r = client.post("/api/backup/restore", headers=AUTH,
                files={"file": ("b.tar.gz", buf.getvalue(), "application/gzip")})
check("a tar that is not a Flipside backup says so",
      r.status_code == 400 and "not a Flipside backup" in r.text, r.text[:160])

print("== inspecting is separate from restoring ==")
r = client.post("/api/backup/inspect", headers=AUTH,
                files={"file": ("b.tar.gz", archive, "application/gzip")})
check("inspect reads the manifest", r.status_code == 200 and r.json()["files"], r.text[:150])
check("and reports when it was taken", r.json().get("created"), r.json())

print("== a restore replaces the audit log too, which is worth knowing ==")
# Not a defect: the audit log is part of the state being restored. But it means
# entries written after the backup was taken are gone afterwards, so anything
# asserting on them has to look after the last restore, not before it.
events = client.get("/api/audit", headers=AUTH).json()["events"]
check("entries recorded after the backup was taken do not survive it",
      not any("downloaded a backup" in e.get("summary", "") for e in events),
      [e.get("summary") for e in events[:4]])

print("== only an admin, and downloading one is audited ==")
check("an unauthenticated caller cannot take a backup",
      client.get("/api/backup").status_code == 401)
client.get("/api/backup", headers=AUTH)
events = client.get("/api/audit", headers=AUTH).json()["events"]
# A GET, so the mutation middleware does not see it — and it hands over the
# signing key, which makes it the most consequential read in the API. Recorded
# by the endpoint itself, the same way the LUKS passphrase reveal is.
check("the download is in the audit log even though it is a GET",
      any(e["path"] == "/api/backup" and "downloaded" in e.get("summary", "")
          for e in events),
      [e for e in events[:4]])
check("so is the restore",
      any(e["path"] == "/api/backup/restore" for e in events),
      [e["path"] for e in events[:8]])

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
