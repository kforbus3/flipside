# Image Builder

Builds a bootable Debian or Ubuntu A/B disk image in a privileged Docker container.

## Usage

```bash
make image HOSTNAME=node USERNAME=admin PASSWORD='ChangeMe123'
# Ubuntu: make image SUITE=noble ...
# or directly:
./builder/run.sh --hostname node --username admin --password 'ChangeMe123' \
    --root-size 3072 --compress zstd
./builder/run.sh --suite noble --hostname node --username admin --password 'ChangeMe123'
```

Output lands in `./output/` (e.g. `debian-trixie-ab.img.zst`, `ubuntu-noble-ab.img.zst`).

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--distro` | auto | `debian` \| `ubuntu`; auto-detected from `--suite` |
| `--suite` | `trixie` | Release: `trixie`, `bookworm` (Debian); `resolute`, `noble`, `jammy` (Ubuntu) |
| `--mirror` | distro default | APT mirror (`deb.debian.org` / `archive.ubuntu.com`) |
| `--arch` | `amd64` | Target architecture |
| `--profile` | `minimal` | `minimal` \| `server` \| `desktop` — named package sets; `minimal` is exactly the base system ([Profiles](#profiles)) |
| `--desktop` | `gnome` | Desktop environment for `--profile desktop`; per-distro list in [Profiles](#profiles). An error without that profile |
| `--hostname` | `debian-ab` | Image hostname |
| `--username` | `debian` | Login user (added to `sudo`) |
| `--password` | `debian` | Password for that user |
| `--root-size` | `3072` | MiB per root slot (raised to a per-distro minimum — see [Root slot sizing](#root-slot-sizing)) |
| `--boot-size` | `512` (`2048` for desktop) | MiB for the shared `/boot` partition — it holds three kernel+initramfs copies, and a desktop initramfs carries DRM firmware |
| `--image-size` | `auto` | Total image size in GiB; `auto` = smallest possible |
| `--packages "a b"` | — | Extra packages to install |
| `--ssh-pubkey FILE` | — | Install an authorized SSH key for the user (from a file) |
| `--ssh-authorized-key K` | — | Same, passing the key inline |
| `--ssh-key-only` | off | Disable SSH password auth (requires a key) |
| `--encrypt` | off | LUKS2-encrypt the root slots and overlay |
| `--unlock METHOD` | `keyfile` | Auto-unlock: `passphrase` \| `keyfile` \| `tpm2` \| `tang`. Note `make image` defaults to `tpm2` instead (`UNLOCK ?= tpm2` in the Makefile) |
| `--tpm2-pcrs LIST` | `7` | PCRs a `tpm2` binding is sealed to |
| `--luks-passphrase P` | — | LUKS passphrase (setup + recovery); required with `--encrypt` |
| `--luks-passphrase-file F` | — | Read it from a file (or `-` for stdin) instead — keeps it out of `ps` |
| `--tang-url URL` | — | Tang server URL (required for `--unlock tang`) |
| `--compress` | `zstd` | `zstd` \| `gzip` \| `none` |
| `--output PATH` | `/output/<distro>-<suite>-ab.img` | Output path (inside the container) |
| `--version STR` | UTC build timestamp | What this build calls itself — reported by each machine to the control plane |

The flags governing writable state and customization — `--state-model`,
`--slot-private-upper`, `--persist`, `--slot-private`, `--volatile`,
`--reset-on-update`, `--keep-path`, `--overlay-min`, `--own-path`,
`--overlay-dir`, `--run-script` — are covered in
[Writable state](#writable-state) and [Customization](#customization) below.

By default the image is built as small as the layout allows (two root slots +
boot + a minimal overlay, ≈7 GiB raw with the defaults) and the overlay
partition auto-expands to fill the real disk on first boot — one image deploys
to any larger disk, and smaller images write faster during mass imaging.

## What's in the image

- Minimal Debian/Ubuntu (`minbase`) + kernel, GRUB, OpenSSH, sudo, RAUC, growpart.
- A login user with sudo; **root is locked** (log in as the user, use sudo).
- `systemd-networkd` configured for DHCP on all ethernet interfaces.
- RAUC preconfigured (`/etc/rauc/system.conf`, `compatible=<distro>-ab`,
  i.e. `debian-ab` or `ubuntu-ab` — update bundles must match).
- `first-boot-expand.service` to grow the overlay on first boot.
- `ab-mark-good.service` — marks the booted slot good before logins are
  permitted, so a slot an update has just written proves itself before anyone
  can bless it by logging in. The fallback is armed only when an update writes
  a slot; proven slots boot outright (see
  [UPDATES.md](UPDATES.md#how-slot-selection-works)).
- `machine-identity.service` — the image ships with a **blank `machine-id` and
  no SSH host keys** (so imaged machines aren't identity clones of each other).
  On first boot each machine generates its own and stores them in the persistent
  overlay, so they survive A/B slot switches and updates.
- Each image ships with `<image>.sha256` (verified by the netboot imager) and a
  `<image>.json` metadata sidecar (distro, release, sizes, encryption) consumed
  by the web UI.

### Root slot sizing

Each of the two root slots holds a complete OS, so the slot — not the image — is
the binding constraint. The builder enforces a per-distro minimum and raises
`--root-size` if you ask for less:

| Distro | Minimum root slot | Why |
|--------|-------------------|-----|
| Debian | 2560 MiB | base system + `linux-image-amd64` + initramfs |
| Ubuntu | 5120 MiB | `linux-image-generic` **depends on** `linux-firmware` and `linux-modules-extra` (~1.7 GiB installed), which Debian never pulls in |
| either, `--profile desktop` | 10240 MiB | the desktop metapackage and its recommends are several GiB installed before the first login — see [Profiles](#profiles) |

Ubuntu needs roughly twice Debian's space for the same install. At Debian's
default of 3072 MiB an Ubuntu build fills the slot partway through initramfs
generation and dies with a bare `No space left on device` from `cpio` — measured,
not theoretical. The minimum is now enforced up front, and if the slot fills
anyway the builder reports the actual usage instead of leaving you to read dpkg
output.

APT's downloaded `.deb` archives are bind-mounted outside the root slot during
installation, so Ubuntu's ~460 MiB of package downloads no longer count against
it.

## Profiles

`--profile` names what the image is *for*, as a curated package set, instead of
a list everyone retypes into `--packages`. The default is `minimal`, which is
**exactly the base system described above** — the flag only names today's
behaviour, so existing builds change in nothing. `--packages` remains additive
with every profile.

```bash
make image PROFILE=server PASSWORD='ChangeMe123'
make image PROFILE=desktop DESKTOP=kde PASSWORD='ChangeMe123'
```

### `server`

What a headless server still lacks after the base install. The base image
already ships `openssh-server`, `curl`, `sudo` and `ca-certificates`, so the
set is deliberately short:

| Package | Why |
|---------|-----|
| `rsync` | moving files and backups on and off the machine |
| `htop` | "what is this machine doing right now" |
| `less` | reading logs — minbase ships no pager at all |
| `nano` | editing config over SSH without vi knowledge |
| `tmux` | a shell that survives the SSH session dropping |

Anything beyond this belongs in `--packages` — including `qemu-guest-agent`,
which is deliberately *not* here: these images deploy to physical machines as
often as VMs.

### `desktop`

Installs a full graphical environment, and the image boots to a **graphical
login**: the metapackage brings a display manager, and the builder sets the
systemd default target to `graphical.target` in the chroot so the login is
graphical even if the package's own postinst did not flip it.

`--desktop` picks the environment; without it the profile defaults to GNOME.
(`--desktop` without `--profile desktop` is an error, not a hint.) Each
distribution curates its own desktop metapackages under different names, and
not every environment exists on both — an unavailable combination is refused
up front with the list that *is* available:

| `--desktop` | Debian installs | Ubuntu installs |
|-------------|-----------------|-----------------|
| `gnome` (default) | `task-gnome-desktop` | `ubuntu-desktop-minimal` |
| `kde` | `task-kde-desktop` | `kde-plasma-desktop` |
| `xfce` | `task-xfce-desktop` | `xubuntu-core` |
| `mate` | `task-mate-desktop` | `ubuntu-mate-core` |
| `cinnamon` | `task-cinnamon-desktop` | — (no Ubuntu flavour) |
| `lxqt` | `task-lxqt-desktop` | `lubuntu-desktop` |

The metapackage is installed **with recommends** — the opposite of everything
else in the image, and on purpose. Debian's `task-*` packages and Ubuntu's
flavour metas carry most of the actual desktop (xorg, the display manager, the
applications) as Recommends, because that is how tasksel installs them;
without recommends a "desktop" image is a few hundred kilobytes of
metapackage and a console login. It also means these pull a **large dependency
tree** — several GiB installed per root slot, and a much longer build.

**Laptops, wifi and networking.** The desktop profile installs NetworkManager
on both distributions, so wired and wifi are managed from the desktop
(`systemd-networkd`, which the other profiles use for DHCP, is disabled for
this one — two DHCP clients on the same NIC fight over the address). On Debian
it also installs `firmware-linux`, `firmware-iwlwifi`, `firmware-realtek` and
`firmware-atheros` — the graphics and wifi firmware laptops actually have; the
image's APT sources already carry the `non-free-firmware` component, so
machines can pull more from the same section later. Ubuntu needs no extra
step: `linux-image-generic` already depends on the complete `linux-firmware`.

**Size.** The root-slot floor for a desktop build is **10240 MiB** (table
above), so with two slots a desktop image is ≈21 GiB raw where a minimal one
is ≈7 GiB. An explicit `--root-size` above the floor is honoured; below it, it
is raised with a warning like any other slot too small for its OS.

The `/boot` partition floor also rises, **512 → 2048 MiB**: it holds three
copies of the kernel and initramfs (the versioned originals plus the per-slot
`/A` and `/B` copies that make rollback carry its own kernel), and a desktop
initramfs is several times a minimal one — `MODULES=most` pulls the DRM
drivers in, and with them the graphics firmware this profile installs. The
builder also checks the space before staging the per-slot copies and refuses
with the `--boot-size` it actually needs, rather than dying on a bare
"No space left on device" mid-copy.

## What is in it: the SBOM

Every build writes three files beside the image:

| file | what it is |
| --- | --- |
| `<image>.spdx.json` | SPDX 2.3 |
| `<image>.cdx.json` | CycloneDX 1.5 |
| `<image>.packages.tsv` | the raw list: package, version, arch, source, source version |

`make-bundle.sh` writes the same three beside each bundle, which matters more:
an update is what *changes* what a machine is running, so an SBOM per image and
none per bundle would describe the fleet as it was first provisioned and never
since.

The list is taken from dpkg's own database while the slot is still mounted —
the only moment it can be read without booting the image. Packages that are
merely `deinstall`ed (config files left behind, software gone) are excluded: an
SBOM naming software that is not there is worse than no SBOM. A copy is left
inside the image at `/usr/lib/flipside/packages.tsv`, so a running machine can
answer the same question about itself.

A build whose SBOM cannot be generated still produces a usable image, loudly —
trading a real artifact for a metadata file would be the wrong way round.

To find something across everything on the server, use **Find a package** on the
Images page, or:

```bash
curl -H "Authorization: Bearer $TOKEN" \
     'http://localhost:8080/api/sbom?package=^openssl$&version=3.0.'
```

`package` is a regular expression; `version` is a substring, because the useful
question is nearly always "which ones are still on 3.0.x". The answer names
images and bundles alike, and reports how many artifacts were searched — so
"no results" cannot be confused with "nothing had an SBOM to search".

## Secure Boot

`--secure-boot auto` (the default) installs the distribution's signed shim and
GRUB and lays them out at the firmware's removable path:

```
firmware --(Microsoft key)--> shim --(distro key, built into shim)--> GRUB
        --(shim lock protocol)--> kernel, which Debian and Ubuntu sign
```

Nothing of yours is signed and nothing has to be enrolled on any machine, which
is the entire reason to use the distribution's chain rather than your own key.

| mode | behaviour |
| --- | --- |
| `auto` | use signed shim and GRUB if the suite has them; carry on without if not |
| `on` | fail the build if they cannot be had |
| `off` | the old unsigned layout |

`on` is worth using in a pipeline: it means a suite that stops shipping signed
shim packages breaks the build rather than quietly producing an image that a
Secure Boot fleet cannot run.

**A Secure Boot image also boots with Secure Boot disabled**, and the BIOS path
is untouched — shim simply runs GRUB without verifying it. There is no machine
this costs anything, which is why it is the default.

**Imaging is the exception.** The netboot imager is a custom initramfs that
nothing signs, so the imaging run itself still needs Secure Boot off. The
sequence on a machine where policy requires it:

1. disable Secure Boot in firmware
2. PXE boot and image the machine
3. re-enable Secure Boot
4. it boots

Whether an image has it is recorded in the sidecar as `secure_boot`, so you can
tell without booting one.

### If it does not boot

The distribution's signed GRUB has its prefix compiled in and looks for
`\EFI\debian\grub.cfg` (or `\EFI\ubuntu\`) on the ESP. The builder writes a
stub there that hands off to the real configuration on the BOOT partition. If a
machine reaches a GRUB rescue prompt with Secure Boot working perfectly, that
stub is what is missing.

## Customization

- **More packages:** `make image PACKAGES="qemu-guest-agent vim curl"` (or
  `--packages "qemu-guest-agent vim curl"` when calling the script directly).
  Also exposed as the "Extra packages" field in the web UI's build form.
- **Bake in files/config:** put them under **`overlay.d/`** at the top of the
  checkout — see [Shipping your own files](#shipping-your-own-files) below.
  (`builder/overlay/` is the project's own, and only its `etc/` and `usr/` trees
  are copied; keep your files out of it so they are not committed.)
- **Run commands inside the image:** `--run-script FILE`, or the script box in
  the web UI's build form.
- **Different base:** `--suite bookworm`, `--suite resolute` (Ubuntu 26.04),
  `--suite noble` (Ubuntu 24.04), `--suite jammy` (Ubuntu 22.04).
- **SSH-key-only login:** pass `--ssh-pubkey` and set a strong throwaway password.

## Shipping your own files

Anything under `overlay.d/` at the top of the checkout is copied over the
image's root filesystem, keeping its path:

    overlay.d/etc/hosts                   ->  /etc/hosts
    overlay.d/etc/netplan/10-corp.yaml    ->  /etc/netplan/10-corp.yaml
    overlay.d/usr/local/bin/site-check    ->  /usr/local/bin/site-check
    overlay.d/opt/agent/agent.conf        ->  /opt/agent/agent.conf

Unlike `builder/overlay/`, the whole tree is copied, not just `etc/` and `usr/`,
and it is applied afterwards — so your version of a file wins over the project's
default. Everything there except its README is gitignored.

The mode is preserved (`cp -a`), so a script shipped `0644` is a script that
does not run on the machine. Nothing warns you: it lands, and it sits there.

If you run the web UI, this directory is editable from the browser — see
[WEBUI.md](WEBUI.md#image-files) — and it is the same directory either way.

### These files also override what is already on the machine

This is the half that is easy to miss. A deployed machine's root is an overlay,
and a file the machine has written shadows the image's copy — so shipping a new
`/etc/hosts` would install it and the machine would carry on reading its own,
with nothing to say so.

Every file in `overlay.d/` is therefore recorded in the image as **image-owned**
(`/usr/lib/ab/image-owned.list`). On the update that delivers it, the machine's
copy **at that exact path** is dropped, so the image's version is what it reads.

Per file, never per directory: shipping one netplan file does not remove the
machine's others. Same path, image wins; everything else is left alone.

Use `--own-path /etc/resolv.conf` (repeatable, or the field in the web UI) to
claim a path you are not shipping a file for.

What does **not** belong here: per-machine identity — hostname, `machine-id`,
SSH host keys — which is generated on first boot and kept in the overlay on
purpose. This is for fleet-wide configuration that should be part of the image.

## Writable state

What a machine is allowed to change, and what the two slots share. The default
suits general-purpose Debian; the other two exist because "shared by default" is
the wrong answer for some fleets, and until now there was no way to say so.

```bash
./builder/run.sh --state-model stateful
```

| `--state-model` | root slot | what survives an update | comparable to |
| --- | --- | --- | --- |
| `overlay` *(default)* | rw, whole root overlaid | everything, including `apt install` | this project's original behaviour |
| `stateful` | **ro** | `/home`, `/var`, `/usr/local`, `/root`, `/srv`, `/opt`; `/etc` overlaid | ChromeOS's stateful partition |
| `appliance` | **ro** | `/data` only; `/var` reverts to the image every slot change | Android `/data`, RAUC/Mender reference layouts |

Under `stateful` and `appliance` the slot is mounted read-only, so nothing a
machine writes can shadow a binary an update just delivered — which is the entire
class of bug that `reset-on-update` exists to clean up after under the default
model. The trade is that `apt install` does not survive an update. That is
correct for an appliance and wrong for a general-purpose server, which is why
the default is what it is.

### Carving out individual paths

All five are repeatable, take absolute paths, and work with any model:

| flag | effect |
| --- | --- |
| `--persist /srv` | shared by both slots, outside the overlay, never shadowed by an update |
| `--slot-private /var/lib/docker` | each slot gets its own; nothing crosses between them |
| `--volatile /var/tmp:256M` | tmpfs; kept neither between slots nor across a reboot |
| `--reset-on-update /var/lib/postgresql` | cleared on a slot change so the new release starts from its own copy |
| `--keep-path /opt/vendor/license` | held back from that clearing |

`--slot-private` is the one worth reaching for first. State whose on-disk format
is tied to a release — a container store, a database directory — is *actively
dangerous* to share: a slot change hands the new binary the old release's data
directory, and nothing warns you. ChromeOS forces a full wipe on version
rollback for exactly this reason.

```bash
./builder/run.sh \
  --slot-private /var/lib/docker \
  --persist /srv \
  --volatile /var/tmp:256M
```

The build validates the result and refuses rather than shipping a manifest that
would only be found wrong at a boot prompt: absolute paths, no duplicates across
directives (including against the model's own), no nested overlays.

### A separate upper layer per slot

```bash
./builder/run.sh --slot-private-upper
```

**The problem it solves.** By default both slots share one overlay upper layer,
so everything the machine has written is there whichever slot boots. That is
what stops an update from taking `/home` with it — and it means a change *you*
made is also there in both. Edit `/etc/fstab` badly, or ship a broken
`/etc/systemd/network` file, and slot A stops booting; boot slot B and it stops
booting too, because it reads the same upper layer. A/B protects you from a bad
*image*, not from a bad *change*, and the difference only shows up when you need
it.

`--slot-private-upper` gives each slot its own upper (`upper-A`, `upper-B` on
the overlay partition, instead of one `upper`). Nothing written while running A
is visible from B. The bad edit is still in A's layer, A still will not boot, and
B comes up exactly as it was — so "boot the other slot" is a real recovery path
for configuration as well as for software.

**What it costs.** The slots stop sharing *everything* the overlay covers. Under
the default model that is the whole root: `/home`, `/etc`, `/opt`, installed
packages. Booting the other slot for the first time gives you a machine that
looks freshly imaged. That is the trade, and it is why this is not the default.

**Use `--persist` for what should stay shared.** A `persist` directive is a bind
outside the overlay, so it is unaffected by this and still crosses the slots:

```bash
./builder/run.sh --slot-private-upper \
  --persist /home \
  --persist /srv \
  --persist /var/log
```

That combination is usually what people actually want: user data and logs follow
the machine, while the configuration that can break a boot stays per-slot.

**What stays shared regardless.** Machine identity is stored on the overlay
partition directly rather than in the upper layer, so `machine-id` and the SSH
host keys are the machine's either way — the other slot is not a different host
to your fleet or to `known_hosts`.

**Interactions worth knowing:**

| | with `--slot-private-upper` |
| --- | --- |
| `--persist PATH` | unchanged; still shared by both slots |
| `--slot-private PATH` | unchanged; it was already per-slot |
| `--volatile PATH` | unchanged; a tmpfs either way |
| `--reset-on-update` | still applies, and still needed — see below |
| `ab-overlay-diff` | reads this slot's layer and says so |
| Recovery: *reset writable state* | sets aside **only the booted slot's** layer |
| Overlay partition usage | up to two copies of what the machine writes |

`reset-on-update` is not made redundant by this. Slot B's upper holds whatever B
wrote the *last* time it ran, which is the previous release — so after an update
replaces B's rootfs, those files would shadow the ones the update just delivered,
exactly as a shared upper would. The clearing still happens, per slot.

**It cannot be changed by an update.** The machine records its layout in
`/var/lib/overlay/.model` (`overlay+per-slot-upper` rather than `overlay`), and
an image declaring the other one is refused at boot, the same way and for the
same reason as a model change: every write the machine has made is in the store
the old layout used, and applying the new one would show an operator an empty
machine. Decide at imaging time.

Verifying it on a running machine:

```console
$ grep upper /usr/lib/ab/state.conf
upper per-slot
$ ls -d /var/lib/overlay/upper*
/var/lib/overlay/upper-A  /var/lib/overlay/upper-B
$ dmesg | grep ab-overlay
ab-overlay: root is now an overlay (lower=slot A, upper=upper-A on the overlay partition)
ab-overlay:   each slot has its own upper layer; nothing written here reaches the other slot
```

## Health checks: what an update has to prove

An A/B update gets one attempt. The slot it wrote is on probation until
something says the boot was good; if nothing does, the next boot falls back to
the slot that was working. By default "good" means the machine booted far enough
to permit logins, which catches the failures rollback exists for — an unbootable
kernel, a broken initramfs, a root that will not mount.

That says nothing about whether your software works. Drop an executable in
`/etc/ab/health.d/` and it does:

```bash
# overlay.d/etc/ab/health.d/10-app.sh   (chmod +x before building)
#!/bin/sh
systemctl is-active --quiet my-agent || { echo "my-agent is not running"; exit 1; }
curl -fsS --max-time 5 http://localhost:8080/healthz >/dev/null || exit 1
```

Every executable in that directory runs in name order. All exit 0 and the slot
is kept. Any one fails and the slot is never marked good, so **the next boot
rolls back to the previous release** — the update is undone without anyone
watching.

Practicalities, all of them learned by watching a real boot:

- **Checks run before logins are permitted.** That is deliberate: it is what
  stops a bad update being blessed by someone logging in and rebooting before
  the checks have had their say. It also means a check that hangs is a machine
  nobody can log into, so the unit has a 60-second `TimeoutStartSec` and hitting
  it counts as a failure.
- **Keep them local and fast.** A check that waits on something across the
  network turns a network problem into a rollback.
- **A non-executable file counts as a failure**, not as absent. Silently
  skipping a check someone believed they had installed is the one outcome worse
  than not having it.
- **A failed check does not block login.** The machine comes up so you can get
  in and see why; it just is not blessed.
- **No checks installed is a pass**, so an image that ships none behaves exactly
  as it did before.

On a running machine, `systemctl status ab-health-check` shows the verdict and
`journalctl -u ab-health-check` shows each check's output.

### Changing the model later

You cannot, by update. A machine records its model, and an image declaring a
different one is refused at boot — the slot boots untouched and the refusal goes
to the kernel log. Everything the machine wrote is still on the partition, but it
is laid out for the old model, and applying the new one would hide it in a way
that is indistinguishable from data loss. Re-image instead. The same applies to
`--slot-private-upper`, which is part of the same recorded identity.

### Seeing it on a running machine

`cat /usr/lib/ab/state.conf` is the manifest. `ab-overlay-diff` lists what the
machine has written and what it is shadowing, and now also lists the paths that
sit outside the overlay entirely, with whether the other slot sees them.

### Running commands in the image

`--run-script FILE` runs a shell script inside the chroot after packages and
both overlays are applied:

```bash
#!/bin/bash
set -euo pipefail
systemctl enable my-agent          # writes symlinks: works
usermod -aG dialout admin
echo "site=hq" > /etc/site.conf
```

It runs as root with the image as `/`, but on the builder's kernel — so
`systemctl enable` works while starting a service does not, because there is no
running init. A non-zero exit fails the build rather than shipping a
half-customized image.

## Building for another architecture

Cross-architecture builds run the target's binaries under qemu, which the kernel
only does once an interpreter is registered with `binfmt_misc`. Docker does not
do that on its own, so building an arm64 image or imager on an amd64 host (or
the reverse) needs it registered first.

Both `run.sh` scripts and the web UI now do it automatically before a cross
build:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
```

It is idempotent and lasts until reboot. The first run on a fresh host pulls
that image, so it needs network; if registration fails the build says so rather
than dying later with `Exec format error`.

Remember that an arm64 **image** needs an arm64 **imager** to be deployed over
the network — the imager is a kernel the target machine executes. Build both.

## How it runs

`builder/run.sh` builds `builder/Dockerfile` and runs it `--privileged` (needed for
loop devices and mounts) with `./output` mounted. The host must be Linux-capable
for loop devices; on Docker Desktop this works inside the Docker VM.

## SSH access

By default the image runs `sshd` and allows password login for the created user
(`root` is locked). To lock it down:

```bash
--ssh-authorized-key "ssh-ed25519 AAAA… you@host" --ssh-key-only
```

`--ssh-key-only` drops a `sshd_config.d` snippet that sets
`PasswordAuthentication no` (so you must supply a key).

## Disk encryption (LUKS2)

`--encrypt` puts the two root slots **and** the overlay inside LUKS2 containers
(the shared `/boot` stays plaintext so GRUB can load the kernel). Pick how each
machine unlocks at boot with `--unlock`:

| Method | Auto-unlock | Key on disk? | Use when |
|--------|-------------|--------------|----------|
| `tpm2` | ✅ (sealed to the TPM) | ❌ | Targets have a TPM 2.0 — **most secure auto-unlock** |
| `tang` | ✅ (from a Tang server) | ❌ | No TPM, but a trusted LAN — **best no-TPM auto-unlock** |
| `keyfile` | ✅ (key on the BOOT partition) | ⚠️ yes | Anywhere, but weak at-rest protection — convenience only |
| `passphrase` | ❌ (prompt at boot) | ❌ | Maximum security, attended boots |

The passphrase you pass is always enrolled as a **recovery** key.

**Where the bootstrap key lives.** On the BOOT partition, at `ab-keys/luks.key`,
not inside the image. `scripts/init-premount/ab-luks-key` copies it into the
initramfs at boot, before cryptroot runs, and crypttab points at that runtime
path.

This is not a detail. The key used to be baked into the image and therefore into
the initramfs — and a bundle carries the rootfs *and* the initramfs built from
it, so an update delivered the *builder's* key. Every encrypted machine given a
bundle from a different build dropped to an initramfs shell:

```
No key available with this passphrase.
ALERT!  LABEL=rootfs-b does not exist.  Dropping to a shell!
```

BOOT is shared by both slots and is not part of a bundle, so the key there is the
machine's own and survives every update. It is no worse protected than before —
it was already on that same plaintext partition, inside the initramfs — and it is
no longer copied into every image and every bundle built from one.

**How tpm2/tang stay unattended *and* keyless:** the bootstrap key above makes the
very first boot unlock on its own. Enrollment then happens over the following two
boots:

1. `luks-enroll` binds every volume to the TPM (or Tang) with **clevis**, checks
   that the binding really recovers the key, rebuilds the initramfs without the
   keyfile in it, and checks *that* — the initramfs must contain the clevis
   unlock hook, the TPM libraries, and no key material. The new initramfs is
   copied into **both** slots. The bootstrap key is still there, untouched.
2. The next boot unlocks through the TPM, because crypttab no longer names the
   bootstrap key and so nothing fetches it. Having proved that,
   `luks-enroll-reap` removes the bootstrap keyslot and deletes
   `/boot/ab-keys/luks.key` — so from then on no key remains on disk.

   The proof is what makes this safe, and it takes care now that no initramfs
   carries a key: `hooks/ab-luks-key` marks an initramfs when crypttab still
   points at the bootstrap key, and the reaper refuses to touch the keyslot
   unless that marker is absent. Without it every initramfs would look keyless
   and the keyslot would be destroyed on a boot the bootstrap key had just
   unlocked — a machine that then never boots again.

If anything fails at any point, the bootstrap keyfile stays and the machine keeps
booting unattended; enrollment retries on the next boot. The key is only ever
destroyed *after* a boot has demonstrated the machine no longer needs it.

> Both methods go through clevis, which is the only mechanism Debian's
> initramfs-tools can call at unlock time. `systemd-cryptenroll` writes a valid
> keyslot that this initrd cannot use — a `tpm2-device=auto` crypttab entry means
> nothing outside a systemd initrd. Images built before this fix enrolled that
> way and could not unlock; see the recovery note in
> [SECURITY.md](SECURITY.md#disk-encryption).

```bash
# TPM2 (recommended where available; UNLOCK defaults to tpm2)
make image ENCRYPT=1 LUKS_PASSPHRASE='recover-me'
# or: ./builder/run.sh --encrypt --unlock tpm2 --luks-passphrase 'recover-me'

# Tang / NBDE
make image ENCRYPT=1 UNLOCK=tang TANG_URL=http://tang.lan:7500 LUKS_PASSPHRASE='recover-me'
# or: ./builder/run.sh --encrypt --unlock tang --tang-url http://tang.lan:7500 --luks-passphrase 'recover-me'
```

> The overlay auto-expand on first boot resizes the LUKS container too.

### Storing the passphrase in a secrets manager

With `tpm2`, `tang` or `keyfile`, the passphrase you pass is *only* a recovery
key: nothing types it again, so nothing exercises it until the day a TPM is
cleared by a firmware update and a machine stops at the initramfs prompt. That
is a bad thing to keep in someone's password note.

The web UI can generate it and file it in OpenBao or HashiCorp Vault for you —
see [WEBUI.md](WEBUI.md#secrets-manager). On the command line,
`scripts/luks-secret.sh` does the same against the `bao`/`vault` CLI and writes
the same payload, so an image built either way is recoverable from the other:

```bash
export BAO_ADDR=https://bao.example.lan:8200 BAO_TOKEN=…

# Generate a passphrase, store it, and stage it where the builder can read it
./scripts/luks-secret.sh new debian-trixie-amd64-ab.img

./builder/run.sh --encrypt --unlock tpm2 \
    --luks-passphrase-file /output/.luks-pass \
    --output /output/debian-trixie-amd64-ab.img

./scripts/luks-secret.sh clean          # remove the staged file
```

`new` writes to the store *before* printing anything, and fails if the store
will not take it — so a build never produces an encrypted image whose recovery
key was not persisted first.

Later, to bundle an update from that image (which needs the passphrase to read
the root slot), or simply to recover a machine:

```bash
./scripts/luks-secret.sh stage debian-trixie-amd64-ab.img   # for the builder
./scripts/luks-secret.sh show  debian-trixie-amd64-ab.img   # for a person
```

The passphrase is staged in a file rather than passed as an argument because
arguments are visible in `ps` to every user on the build host; `output/` is
already mounted into the builder, so the file needs no extra plumbing. The
store is keyed on the image name with any `.zst`/`.gz` suffix stripped, so
changing `--compress` does not strand the entry.

Configuration comes from the environment, as both CLIs expect: `BAO_ADDR` /
`VAULT_ADDR`, `BAO_TOKEN` / `VAULT_TOKEN`, plus `LUKS_KV_MOUNT` (default
`secret`) and `LUKS_KV_PREFIX` (default `debian-ab-images`).

## Notes & limitations

- **Boots on both BIOS and UEFI.** GRUB is installed twice: `i386-pc` into the
  bios_grub partition, and `x86_64-efi` onto the ESP at the removable path
  (`\EFI\BOOT\BOOTX64.EFI`, no NVRAM entry needed — right for mass imaging).
  Both share the same `grub.cfg` and `grubenv` on the BOOT partition, so A/B
  slot logic behaves identically under either firmware.
- **Secure Boot works for the deployed machine, but not while imaging it.** A
  built image carries the distribution's signed shim and GRUB and boots with
  Secure Boot enabled (see below). The *netboot imager* is a custom initramfs
  that nothing signs, so Secure Boot has to be off for the imaging run itself —
  disable it, image the machine, turn it back on.
- `/boot` and the kernel are shared across A/B; A/B applies to the root
  filesystem. A bad kernel affects both slots — test kernel changes before
  rolling out. See [UPDATES.md](UPDATES.md).
