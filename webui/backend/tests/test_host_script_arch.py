#!/usr/bin/env python3
"""A per-machine iPXE script must boot the imager the machine can actually run.

There are two iPXE scripts in this system and they had drifted. The fallback
served to unassigned machines (server/http/default.ipxe.tmpl) picks the imager
directory from iPXE's own ${buildarch} and guards both fetches with
`|| goto noimager`. The per-machine script the web UI generates did neither: it
hardcoded /imager/, so an arm64 machine WITH an assignment was handed the amd64
imager, while the same machine left unassigned booted correctly -- and a missing
imager aborted the script instead of saying so and rebooting.

An imager of the wrong architecture does not fail with a message about
architecture. It fails the way any unbootable initramfs does: the kernel finds no
working init and panics, naming nothing.
"""
import os
import sys
import tempfile

PROJ = tempfile.mkdtemp()
OUT = os.path.join(PROJ, "output")
os.makedirs(OUT, exist_ok=True)
os.environ.update(PROJECT_DIR=PROJ, STATIC_DIR="/tmp/none",
                  ADMIN_PASSWORD="ci", SECRET_KEY="ci-secret-key")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import orchestrator as orch  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name} {extra}")


def script_for(mac):
    path = os.path.join(orch.hosts_dir(), mac.replace(":", "-") + ".ipxe")
    with open(path) as f:
        return f.read()


open(os.path.join(OUT, "test.img"), "w").close()
MAC = "aa:bb:cc:dd:ee:11"
orch.write_assignments([{"mac": MAC, "image": "test.img"}])
s = script_for(MAC)
kernel_line = [l for l in s.splitlines() if l.startswith("kernel")][0]
initrd_line = [l for l in s.splitlines() if l.startswith("initrd")][0]

print("== the machine chooses its own imager architecture ==")
check("the script branches on ${buildarch}", "iseq ${buildarch} arm64" in s, s)
check("the kernel path is the branch result, not a hardcoded directory",
      "${imgdir}/vmlinuz" in kernel_line, kernel_line)
check("the initrd path likewise", "${imgdir}/initramfs.img" in initrd_line, initrd_line)
# The bug, stated directly: an arm64 machine must not be sent the amd64 imager.
check("no hardcoded /imager/ path survives",
      "/imager/vmlinuz" not in s and "/imager/initramfs.img" not in s, s)

print("== ${buildarch} is left for iPXE to expand ==")
# This file is written once and served to every machine that asks. Expanding the
# variable here would bake one machine's architecture into all of them.
check("buildarch is not substituted at write time", "${buildarch}" in s)
check("imgdir is not substituted at write time", "${imgdir}" in s)

print("== a missing imager says so instead of aborting ==")
check("the kernel fetch is guarded", kernel_line.rstrip().endswith("|| goto noimager"), kernel_line)
check("the initrd fetch is guarded", initrd_line.rstrip().endswith("|| goto noimager"), initrd_line)
has_label = "\n:noimager" in s
check("the label the guards jump to exists", has_label, s)
# Guarded rather than indexed: without the label this raised IndexError and took
# the remaining checks down with it, so a regression reported a crash instead of
# a list of what broke.
check("it reboots rather than leaving the machine at a prompt",
      has_label and "reboot" in s.split(":noimager", 1)[1])

print("== the assignment's own fields still reach the machine ==")
orch.write_assignments([{"mac": MAC, "image": "test.img", "hostname": "web01"}])
s = script_for(MAC)
kernel_line = [l for l in s.splitlines() if l.startswith("kernel")][0]
check("the image is still passed", "imager.url=" in kernel_line and "test.img" in kernel_line)
check("the hostname is still passed", "imager.hostname=web01" in kernel_line)
check("the guard did not swallow a parameter",
      kernel_line.count("|| goto noimager") == 1, kernel_line)

print("== the control-plane address reaches the machine, and only when set ==")
# The same drift this file exists for, in a second place. Every other parameter
# on that command line points at SERVER_IP -- the provisioning segment, the one
# address the machine is guaranteed to lose the moment it is unracked. If
# imager.control= is missing, the machine writes the provisioning address into
# its boot marker, the agent adopts it, and the machine never checks in again
# from anywhere it actually lives. Nothing reports that: it images perfectly and
# is simply never heard from.
check("no control parameter when none is configured", "imager.control=" not in kernel_line,
      kernel_line)
cfg = orch.read_env()
cfg["CONTROL_URL"] = "https://flipside.example.com/"
orch.write_env(cfg)
orch.write_assignments([{"mac": MAC, "image": "test.img", "hostname": "web01"}])
kernel_line = [l for l in script_for(MAC).splitlines() if l.startswith("kernel")][0]
check("the configured control URL is passed",
      "imager.control=https://flipside.example.com " in kernel_line + " ", kernel_line)
check("with the trailing slash stripped, so the agent does not build a // URL",
      "imager.control=https://flipside.example.com/ " not in kernel_line, kernel_line)
check("and it did not displace anything else",
      "imager.hostname=web01" in kernel_line and "imager.url=" in kernel_line
      and kernel_line.count("|| goto noimager") == 1, kernel_line)

print("== the two iPXE scripts still agree ==")
# The generated script and the fallback template are two implementations of one
# thing, and this file exists because they drifted once already.
tmpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "..", "..", "server", "http",
                         "default.ipxe.tmpl")).read()
check("the fallback template also carries a control parameter",
      "${CONTROL_ARG}" in tmpl, tmpl)
check("and it is on the kernel line, before the console arguments",
      any(l.startswith("kernel") and "${CONTROL_ARG} console=" in l
          for l in tmpl.splitlines()), tmpl)

print()
print(f"{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
