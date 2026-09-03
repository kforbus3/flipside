# Flipside

Build a Debian or Ubuntu A/B (dual-root) disk image once, then **netboot a whole
switch full of machines and image them all at once** — unattended. Updates are
signed, atomic, and health-gated: a healthy machine keeps its side, and a bad
update boots the flip side. Designed for IT departments and homelabs that need
to provision many identical machines quickly and reliably.

*Formerly `debian-ab-images` — old clone URLs redirect.*

![CI](https://github.com/kforbus3/flipside/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)
![Docker](https://img.shields.io/badge/docker-compose-blue.svg)

---

## What you get

| Component | What it does |
|-----------|--------------|
| **builder/** | Produces a bootable Debian **or Ubuntu** A/B disk image (`.img`) — two root slots, shared `/boot`, persistent overlay, GRUB+RAUC for atomic updates, first-boot auto-expand. Runs in Docker. |
| **imager/** | Builds a tiny netboot environment (kernel + initramfs) that auto-detects a machine's disk, streams the image over HTTP, writes it, and reboots. Built per architecture — an amd64 imager cannot boot an arm64 machine. |
| **server/** | A Dockerized provisioning server: dnsmasq (proxyDHCP/DHCP + TFTP + iPXE) and nginx (serves the image). Plug machines into the switch, power on, walk away. |
| **webui/** | An optional browser UI to manage everything — build images with a **live log**, manage the image library, configure/run the provisioning server, **watch machines being imaged in real time**, track the **fleet** it has deployed, and build signed **update bundles**. |

```
                              ┌─────────── provisioning server (Docker) ───────────┐
   [ switch ]                 │  dnsmasq  ── proxyDHCP/DHCP + TFTP + iPXE chainload  │
   machine 1  ──PXE boot──▶   │  nginx    ── serves imager kernel/initramfs + image │
   machine 2  ──PXE boot──▶   └────────────────────────────────────────────────────┘
   machine N  ──PXE boot──▶          │
        ▲                            ▼
        │                    each machine boots the imager, which
        └──── reboots ◀──────  writes the A/B image to its local disk
          into the installed
             A/B system
```

## The A/B image layout

```
GPT:
  p1  bios_grub   1 MiB    GRUB BIOS core
  p2  ESP         128 MiB  EFI system partition (GRUB UEFI)  (label EFI)
  p3  BOOT        512 MiB  shared /boot, kernel, grubenv     (label BOOT)
  p4  rootfs-a    N GiB    root slot A (Debian/Ubuntu)       (label rootfs-a)
  p5  rootfs-b    N GiB    root slot B (copy of A)           (label rootfs-b)
  p6  overlay     rest     persistent data /var/lib/overlay  (grows on first boot)
```

- **Boots on BIOS and UEFI, with Secure Boot** — GRUB is installed for both
  (`i386-pc` in the bios_grub partition, and the distribution's signed shim and
  GRUB at the removable path `\EFI\BOOT\BOOTX64.EFI`), so the same image works
  on legacy firmware, on modern firmware, and on a machine where Secure Boot is
  mandated. Nothing of yours needs signing and nothing needs enrolling.
  Imaging itself still needs Secure Boot off — the netboot imager is an unsigned
  initramfs — so the sequence is: disable, image, re-enable.
- **A/B roots** let you update atomically: write the inactive slot, flip the
  GRUB boot order, reboot. Both slots are populated at build time.
- **Root is an overlay**: the slot is the read-only lower layer and everything
  written since imaging lands on the overlay partition. So `/home` is as large
  as the disk rather than the root slot, and an update replaces the OS without
  destroying user data. The upper layer is shared by both slots, which means a
  change you make in one slot is still there in the other — when that is the
  problem, the GRUB menu has **Recovery** entries to reset it or bypass it, and
  `ab-overlay-diff` on the machine shows what changed. See
  [docs/RECOVERY.md](docs/RECOVERY.md). Or build with `--slot-private-upper` and
  each slot gets its own upper layer, so a change that stops slot A booting
  cannot follow you into slot B.
- **…but that is a default, not the design.** The image ships a manifest
  (`/usr/lib/ab/state.conf`) saying which paths are writable and which of them
  the two slots share, and the initramfs applies it. `--slot-private
  /var/lib/docker` keeps release-coupled state apart; `--state-model stateful`
  makes `/usr` read-only and enumerates what a machine owns, the way ChromeOS
  does; `--state-model appliance` keeps only `/data` across an update, the way
  Android and the RAUC/Mender reference layouts do. See
  [docs/BUILDER.md](docs/BUILDER.md#writable-state).
- **GRUB + RAUC** integration: slot selection lives in `grubenv`; [RAUC](https://rauc.io/)
  is preconfigured (`compatible=<distro>-ab`) for signed bundle updates.
- **Smallest possible image** by default: the image is sized to its contents and
  the **persistent overlay auto-expands** to fill the target disk on first boot —
  so one image works on any disk size, and imaging is as fast as possible.
- **Unique machine identity**: the image ships with a blank `machine-id` and no
  SSH host keys; each machine generates its own on first boot and keeps them
  across A/B updates (stored in the overlay).
- **Build profiles** — `minimal` (the default: exactly the base system),
  `server` (a small headless-admin set), or `desktop` (a full graphical login:
  GNOME/KDE/Xfce/MATE/Cinnamon/LXQt on Debian, the flavour metas on Ubuntu,
  with NetworkManager and wifi firmware for laptops) — see
  [docs/BUILDER.md](docs/BUILDER.md#profiles).

## Quick start

### 1. Build the image

```bash
make image HOSTNAME=node USERNAME=admin PASSWORD='ChangeMe123'
# → output/debian-trixie-ab.img.zst

# Ubuntu instead? Pick an Ubuntu release with SUITE:
make image SUITE=noble HOSTNAME=node USERNAME=admin PASSWORD='ChangeMe123'
# → output/ubuntu-noble-ab.img.zst

# Need extra packages baked into the image? Add PACKAGES:
make image PACKAGES="qemu-guest-agent vim curl" HOSTNAME=node
```

The image is built as small as possible by default (about 7 GiB raw with the
default slot sizes, far less compressed) and expands to fill each machine's
disk on first boot; set `IMAGE_SIZE=<GiB>` to force a fixed size.

Supported releases: Debian `trixie` (13) and `bookworm` (12); Ubuntu `resolute`
(26.04 LTS), `noble` (24.04 LTS), and `jammy` (22.04 LTS). The builder also
accepts the other Ubuntu suites it can recognize (`bionic`, `focal`,
`oracular`, `plucky`, `questing`), but those are untested here — the listed
releases are the ones the boot tests cover.

### 2. Build the netboot imager

```bash
make imager                       # amd64 → output/imager/
./imager/run.sh --arch arm64      # arm64 → output/imager/arm64/
```

The imager is a kernel the target machine executes, so it has to match that
machine: an amd64 imager cannot netboot an arm64 box. Build one per architecture
you deploy. Each machine picks the right one at boot from iPXE's `${buildarch}`,
so both can be present at once.

### 3. Start the provisioning server

```bash
cd server
cp .env.example .env
# Edit .env: set SERVER_IP, INTERFACE, IMAGE_FILE, and MODE (dhcp or proxy).
docker compose up -d --build
```

### 4. Image the machines

Plug the target machines into the same switch, set them to **network boot** (PXE),
and power them on. Each one boots the imager, writes the image to its local disk,
and reboots into the A/B system — no keyboard required. Watch progress with:

```bash
make server-logs
```

## Web UI (manage everything from the browser)

Prefer a UI over the command line? Run the management console:

```bash
cp webui/.env.example webui/.env   # set ADMIN_PASSWORD and SECRET_KEY
make webui
```

Open **http://localhost:8080** to build images (with a live build log), manage the
image library, configure and start the provisioning server, and watch machines get
imaged in real time. The files baked into every image (`overlay.d/`) are editable
from the browser too, so site-specific configuration does not need a shell on the
build host. It can also connect to a secrets manager (OpenBao or
HashiCorp Vault) so encrypted builds generate their own LUKS recovery passphrase
and file it under the image's name, rather than someone inventing one and keeping
it in a note. See [docs/WEBUI.md](docs/WEBUI.md).

## DHCP modes

The Provisioning page lists the host's network interfaces; pick the one facing
the machines and everything else — server IP, subnet, lease range — is derived
from it. DHCP and TFTP are then bound to **that interface alone**, so they
cannot answer on, or interfere with, any other network the host is attached to.

- **`dhcp`** (default) — *standalone and self-contained*. This server owns the
  provisioning network and hands out its own leases. Nothing else on the segment
  is required. Use a dedicated NIC, switch, or VLAN.
- **`proxy`** — *proxyDHCP*. Answers only PXE boot questions and leaves IP leases
  to the network's existing DHCP server. Use when the machines have to stay on a
  LAN you don't control.

Both modes serve iPXE, the imager, and the image from this app — nothing is
fetched from the internet at boot time. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for full configuration.

## Safety

Network imaging **overwrites the target disk**. The imager selects the largest
non-removable disk by default; pin a specific disk with `imager.disk=/dev/sdX`
(see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)). Only run the provisioning server on a
network where you intend every PXE-booting machine to be re-imaged.

## Documentation

- **[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) — installation and user guide (start here)**
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the pieces fit together
- [docs/WEBUI.md](docs/WEBUI.md) — the browser-based management console
- [docs/BUILDER.md](docs/BUILDER.md) — image build options and customization
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — provisioning server, DHCP modes, real-hardware + QEMU testing
- [docs/UPDATES.md](docs/UPDATES.md) — building signed update bundles, installing them, and watching a rollout
- **[docs/RECOVERY.md](docs/RECOVERY.md) — the overlay root, and the GRUB recovery entries when a change breaks a machine**
- [docs/SECURITY.md](docs/SECURITY.md) — secrets, signing, network exposure
- [CONTRIBUTING.md](CONTRIBUTING.md)

## Requirements

- A Linux host with Docker (the builder needs `--privileged` for loop devices).
- For the provisioning server: a host on the imaging LAN (host networking).

## License

Licensed under the **Apache License, Version 2.0**. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
