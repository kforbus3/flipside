# Getting Started — Installation & User Guide

This guide takes you from a fresh clone to a switch full of freshly imaged
machines. It covers installation, building your first image, setting up the
provisioning server, imaging machines, and day-2 operations. Skim the parts you
need — each section stands alone.

- [1. What this project does](#1-what-this-project-does)
- [2. Requirements](#2-requirements)
- [3. Installation](#3-installation)
- [4. Build your first image](#4-build-your-first-image)
- [5. Build the netboot imager](#5-build-the-netboot-imager)
- [6. Set up the provisioning server](#6-set-up-the-provisioning-server)
- [7. Image your machines](#7-image-your-machines)
- [8. First boot: what to expect](#8-first-boot-what-to-expect)
- [9. Using the web UI instead](#9-using-the-web-ui-instead)
- [10. Day-2 operations](#10-day-2-operations)
- [11. Troubleshooting](#11-troubleshooting)

## 1. What this project does

You build one **A/B disk image** (Debian or Ubuntu, two root slots for atomic
updates), then **netboot any number of machines** from a small provisioning
server; each machine writes the image to its local disk and reboots into a
ready-to-use system. Images boot on **both BIOS and UEFI** firmware, expand to
fill whatever disk they land on, and each machine gets its own SSH host keys
and machine-id on first boot.

## 2. Requirements

| What | Why |
|------|-----|
| A Linux host (or Docker Desktop) with **Docker + Compose** | Everything runs in containers; the builder needs `--privileged` for loop devices |
| ~16 GiB free disk in the repo's `output/` | Raw image + compressed image + netboot files (the builder warns below this for the default build) |
| For imaging: a host attached to the **imaging LAN/switch** | The provisioning server uses host networking for DHCP/TFTP |
| Target machines that can **PXE boot** (BIOS or UEFI) | Secure Boot must be off **while imaging**; the installed image boots with it back on |

The builder, imager, and server are `linux/amd64` images and produce amd64
systems; on Apple Silicon or other arm64 hosts they run under emulation
(slower, but working).

## 3. Installation

```bash
git clone https://github.com/kforbus3/flipside.git
cd flipside
make help          # see every available target
```

That's it — there is nothing else to install; every tool runs inside Docker.

## 4. Build your first image

```bash
make image HOSTNAME=node USERNAME=admin PASSWORD='ChangeMe123'
# → output/debian-trixie-ab.img.zst  (+ .sha256 and .json sidecars)
```

The default is a minimal headless system; `PROFILE=server` adds a small
server toolkit and `PROFILE=desktop DESKTOP=kde` builds a full graphical
desktop instead.

Key variables (all optional; see [BUILDER.md](BUILDER.md) for the full list):

| Variable | Default | Meaning |
|----------|---------|---------|
| `SUITE` | `trixie` | `trixie`/`bookworm` (Debian), `resolute`/`noble`/`jammy` (Ubuntu) |
| `USERNAME` / `PASSWORD` | `debian`/`debian` | Login user (gets sudo; root is locked). **Always set a real password.** |
| `PROFILE` / `DESKTOP` | `minimal` | What the image is for: `minimal` (the base system), `server` (headless tools), or `desktop` with `DESKTOP=gnome`/`kde`/… for a graphical login — see [BUILDER.md](BUILDER.md#profiles) |
| `PACKAGES` | — | Extra packages baked into the image, e.g. `PACKAGES="vim curl qemu-guest-agent"` |
| `IMAGE_SIZE` | `auto` | `auto` builds the smallest possible image (≈7 GiB raw); it expands on first boot |
| `ROOT_SIZE` | `3072` | MiB per root slot — raise it if you bake in large package sets |
| `ENCRYPT=1` + `LUKS_PASSPHRASE` | off | LUKS2 full-disk encryption; see [BUILDER.md](BUILDER.md#disk-encryption-luks2) |

Common recipes:

```bash
# Ubuntu 26.04 LTS with extra packages
make image SUITE=resolute PACKAGES="qemu-guest-agent htop" PASSWORD='ChangeMe123'

# SSH-key-only image (no password login over SSH)
./builder/run.sh --password 'ChangeMe123' \
  --ssh-authorized-key "ssh-ed25519 AAAA… you@host" --ssh-key-only

# Encrypted image, auto-unlocked by each machine's TPM
make image ENCRYPT=1 LUKS_PASSPHRASE='recovery-phrase' PASSWORD='ChangeMe123'
```

## 5. Build the netboot imager

The imager is the tiny kernel + initramfs that target machines boot over the
network; it downloads the image, writes it to disk, and reboots.

```bash
make imager
# → output/imager/{vmlinuz,initramfs.img}
```

Rebuild it only when you want a newer kernel — it is independent of the images
it deploys.

## 6. Set up the provisioning server

Run this on a host attached to the switch/VLAN where the machines will boot:

```bash
cd server
cp .env.example .env
$EDITOR .env
docker compose up -d --build      # or: make server-up (from the repo root)
```

The four settings that matter:

- **`SERVER_IP`** — this host's IP on the imaging LAN.
- **`INTERFACE`** — the NIC facing the machines being imaged. Required: DHCP
  and TFTP bind to it alone, and the server refuses to start without it rather
  than answering DHCP on every network the host is attached to.
- **`IMAGE_FILE`** — which file in `output/` to deploy (e.g.
  `debian-trixie-ab.img.zst`).
- **`MODE`** — how DHCP works:
  - `dhcp` (default): standalone DHCP for an isolated provisioning switch. Set
    `DHCP_RANGE_START`/`DHCP_RANGE_END`. Never use on a LAN that already
    has DHCP.
  - `proxy`: your existing router keeps handing out IPs; this server
    only answers PXE questions. Safe on a shared LAN. Set `PROXY_SUBNET`
    (e.g. `192.168.1.0`).

> ⚠️ Anything that PXE-boots on this network **will be re-imaged** (its disk
> overwritten). Use a dedicated switch/VLAN, or `MODE=proxy` on a network where
> no other machine is set to network-boot.

## 7. Image your machines

1. Plug the targets into the switch.
2. In each machine's firmware: enable **network/PXE boot** (and on UEFI,
   **disable Secure Boot for the imaging run** — the netboot imager is an
   unsigned initramfs; turn it back on afterwards and the installed image boots
   with it). Both BIOS and UEFI machines work, including mixed
   batches.
3. Power on and walk away. Each machine PXE-boots the imager, streams the
   image, writes it, and reboots into the installed system.

Watch progress from the server:

```bash
make server-logs        # dnsmasq (DHCP/TFTP) + nginx (image downloads)
```

Prefer pinning the target disk? Add `imager.disk=/dev/nvme0n1` to the kernel
command line in `server/http/boot.ipxe.tmpl` — by default the imager picks the
largest non-removable disk. All imager options are listed in
[DEPLOYMENT.md](DEPLOYMENT.md#imager-command-line-options).

## 8. First boot: what to expect

On its first boot, each freshly imaged machine automatically:

1. **Expands** the overlay partition (and its LUKS container, if encrypted) to
   fill the local disk.
2. **Generates its own identity** — a fresh `machine-id` and SSH host keys —
   and stores them in the persistent overlay, so they stay stable across A/B
   updates. (Expect the SSH host key to be new the first time you connect.)
3. If encrypted with `tpm2`/`tang`: **enrolls** the LUKS volumes to the TPM or
   Tang server and stages a keyless initramfs. The bootstrap keyfile is destroyed
   one boot later, once that boot has proved the machine can unlock without it.
4. Marks the booted slot "good". The A/B fallback itself is armed only when an
   update writes a slot — a freshly imaged machine boots proven slots outright.
   See [UPDATES.md](UPDATES.md#how-slot-selection-works).

Then log in as the user you baked in (`USERNAME`/`PASSWORD` or your SSH key).
Root is locked; use `sudo`.

## 9. Using the web UI instead

Everything above (building, image library, server config, live imaging monitor)
is also available in a browser:

```bash
cp webui/.env.example webui/.env   # set ADMIN_PASSWORD and SECRET_KEY
make webui
```

Open <http://localhost:8080>, log in as username **`admin`** with your
`ADMIN_PASSWORD` (further users and roles can be added under **Users** once
you are in), and use the **Build** page
(live log), **Images** library, and **Provisioning** pages. Details in
[WEBUI.md](WEBUI.md).

## 10. Day-2 operations

- **Roll out a new image version** to machines already in the field: build a
  RAUC bundle and install it — the inactive slot is written, the machine
  reboots into it, and falls back automatically if it fails. See
  [UPDATES.md](UPDATES.md).
- **Re-image from scratch**: leave the provisioning server up and PXE-boot the
  machine again (its disk is rewritten; it gets a fresh identity).
- **Serve a different image**: drop the new file in `output/`, change
  `IMAGE_FILE` in `server/.env`, and `docker compose up -d`.
- **Test images without hardware** in QEMU — copy-paste commands in
  [DEPLOYMENT.md](DEPLOYMENT.md#testing-without-hardware-qemu).

## 11. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Machine PXE-boots but gets no answer | `SERVER_IP`/`PROXY_SUBNET` wrong, or the server isn't on the same L2 segment; check `make server-logs` |
| UEFI machine won't boot the imager | Secure Boot is enabled — turn it off to image, then back on; the installed image supports it |
| Imager writes the wrong disk | Pin it: `imager.disk=/dev/…` in `boot.ipxe.tmpl` |
| "checksum mismatch" during imaging | Stale `.sha256` sidecar — rebuild or re-copy the image and its sidecars together |
| Imaged machine boots to GRUB but no OS | Image/firmware mismatch is *not* possible (hybrid boot); check the disk actually finished writing (imager log on the console) |
| Build fails with "Permission denied" on loop devices | The builder container must run `--privileged` (the Make targets already do) |
| Web UI won't start | `ADMIN_PASSWORD` and `SECRET_KEY` must both be set in `webui/.env` |
| Build fails: `unable to prepare context: path "/project/builder" not found` | The repo isn't mounted into the UI container. Clear `HOST_PROJECT_DIR` in `webui/.env` (it is auto-detected) and run `make webui` from the repo root. The Build page shows the same diagnosis in a banner. |

Still stuck? Open an issue with the relevant log
(`output/build.log`, `make server-logs`, or the imager's console output).
