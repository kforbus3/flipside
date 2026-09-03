# Changelog

Notable changes per release. Dates are the tag date.

## Unreleased

**Fleet management, and the enterprise gaps around it.** Six things a fleet of
this needed and did not have.

- **A control plane the fleet pulls from.** `ab-update` could update one
  machine, from that machine; there was no way to update a fleet, and no way to
  know one was still out there — the boot check-in fired once, at boot, so a
  machine that died three weeks after imaging read `running` forever. Machines
  now run `ab-agent.timer`, which reports in every five minutes and receives a
  directive back. Nothing is ever pushed: a machine is imaged on a private
  provisioning switch and then moved somewhere this server cannot reach, so the
  server records what each machine *should* run and each machine asks.
  - **Host groups**, set by operators — an agent cannot put itself in one.
    Targets resolve live, so a machine imaged into a group today is picked up
    by a rollout that started yesterday.
  - **Staged rollouts**: canary, then a soak, then batches, halting on a
    failure budget, optionally inside a maintenance window. A machine counts as
    done only when it comes back **on the new version and healthy** — not when
    the install returns zero. A bundle that installs and then fails to boot is
    the failure the A/B layout exists to survive, and a rollout that counted
    the install would walk it across the whole fleet while every machine
    quietly rolled back.
  - **Presence** — online / stale / offline, with "no agent" kept distinct from
    offline: a machine that never ran one has not gone quiet.
  - **The imaging-address trap is closed.** `/boot/ab-deploy.json` recorded the
    provisioning address — the one address a machine is guaranteed to lose.
    `CONTROL_URL` is written onto machines at imaging time *and* re-advertised
    in every check-in reply, so an existing fleet re-points itself without
    anyone visiting a machine.

- **An SBOM per image and per bundle.** The sidecar recorded distro, suite,
  profile and sizes, and nothing about what was *in* the image — so "which of
  our images carry the vulnerable openssl" meant mounting each one. SPDX 2.3,
  CycloneDX 1.5 and a raw TSV are written beside every artifact, taken from
  dpkg while the slot is still mounted. `GET /api/sbom?package=` searches all
  of them at once and reports how many were searched, so "no results" cannot be
  read as "your fleet is clear" when the truth is "nothing has an SBOM".

- **Metrics, structured logs, and an audit trail that leaves the box.**
  `/metrics` in Prometheus format (behind the viewer role by default — it is a
  map of the estate), `LOG_JSON=true` for one JSON object per line, and
  `AUDIT_SYSLOG` / `AUDIT_HTTP_URL` to ship every audit event as it happens.
  Delivery never blocks a request: the local `audit.jsonl` is bounded and
  trimmed, so it is a buffer rather than an archive — and it lives on the
  machine being audited.

- **UEFI Secure Boot on the installed image.** `--secure-boot auto` (the new
  default) installs the distribution's signed shim and GRUB, so a built image
  boots where Secure Boot is mandated. Nothing of yours is signed and nothing
  is enrolled on any machine. **Imaging still needs it off** — the netboot
  imager is an unsigned initramfs — so the sequence is disable, image,
  re-enable.

- **The web UI no longer holds the Docker socket.** An allowlisting proxy
  (`dockerproxy/`) passes the container-lifecycle and build calls this project
  makes and refuses the rest: exec, attach, arbitrary images, host bind mounts,
  the socket itself, privilege for anything but the builder. It does **not**
  make the UI non-root-equivalent — the image builder must run privileged, so
  anyone who can start a build can reach host root, and the documentation now
  says so plainly instead of implying the socket was the whole problem.

- **Backup and restore in one command.** `make backup` / `make restore FILE=`,
  a Backup page, and `scripts/flipside-backup.sh` for when the web UI is the
  thing that is broken. The documentation used to list six paths and say "put
  the files back"; that list grew by four entries in this release alone. The
  restore verifies every file against its recorded checksum before writing
  anything, so a damaged archive changes nothing.

## v0.9.1 — 2026-08-17

**A desktop build gets the `/boot` it actually needs.** The first real desktop
build died at the per-slot kernel copy with a bare "No space left on device" —
one line after GRUB reported no error. `/boot` holds three copies of the
kernel and initramfs (the versioned originals plus the `/A` and `/B` copies
that make rollback carry its own kernel), and a desktop initramfs is several
times a minimal one: `MODULES=most` pulls the DRM drivers in, and with them
the graphics firmware the profile installs. The desktop profile now floors
`/boot` at 2048 MiB the same way it floors the root slot at 10 GiB,
`--boot-size` is a real documented flag rather than an undocumented
environment variable, and the per-slot copy is preceded by a space check that
names the initramfs size and the `--boot-size` that would fit. Minimal and
server builds are unchanged.

## v0.9.0 — 2026-08-17

**The web UI has real identity.** One shared admin password with
root-equivalent reach was the single genuine blocker between this project and
an enterprise access review. Now:

- **Named users with roles** — `admin` > `operator` > `viewer`, declared per
  endpoint in every router. Viewers read, operators run the day job (builds,
  imaging, updates), admins hold users, tokens, secrets and server config.
  The UI disables what your role cannot do; the API answers 403 with the
  required rank named. Passwords are bcrypt hashes in `output/users.json`
  (0600); the last enabled admin cannot be deleted, demoted or disabled.
- **Sessions that can be taken back** — opaque `fls_` tokens stored as
  SHA-256 server-side (a stolen state file yields no working session),
  12-hour sliding expiry capped at 7 days, surviving restarts, individually
  revocable, and checked against the live user record so disabling a user
  ends their access now — not at token expiry. A JWT cannot be taken back;
  that is why these are not JWTs.
- **API tokens** — `flt_` bearer tokens for automation: role-ceilinged,
  shown once, stored hashed, revocable.
- **An audit log** — `output/audit.jsonl`, bounded and append-only: every
  mutating API call with actor, role, status and source IP; failed logins
  with the attempted username; the LUKS passphrase reveal recorded
  explicitly. Machine report endpoints are excluded so a fleet imaging
  itself cannot drown the record. Admin viewer page included.
- **OIDC single sign-on** — set `OIDC_ISSUER` and `OIDC_CLIENT_ID` and the
  login page grows an SSO button: authorization-code + PKCE against any
  conforming IdP, roles mapped from the group claim (highest wins, refreshed
  every login), unmapped users refused by default with an audit line. A
  username collision with a local user is a refusal, never a merge — that
  would be an account takeover with extra steps. A down IdP costs exactly
  the SSO button; password login never depends on it.

**The upgrade is invisible to existing deployments.** On first boot with no
users file, `ADMIN_PASSWORD` becomes a real, hashed `admin` user — and is
never consulted again, so a password changed in the UI is not reverted by an
environment variable on the next restart. A corrupt users file reads as
empty, never as missing: the bootstrap must not resurrect old credentials
over repaired ones.

160 new checks across three test files, including a full OIDC flow against
an in-process fake IdP whose token endpoint really verifies the PKCE
challenge.

## v0.8.0 — 2026-08-17

**Images now come in profiles: Minimal, Server, and Desktop.** Fleets are not
only headless machines — images deploy to desktops and laptops where a
graphical login is expected, and the only route there was hand-typing package
lists nobody could remember. `--profile` (and a Profile select in the web UI)
names the intent:

- **minimal** — exactly the base system this builder has always produced. The
  flag only names it; existing builds change in nothing, which a test now
  proves.
- **server** — the small headless-admin set the base still lacks: `rsync htop
  less nano tmux`. openssh-server and curl were already in the base; anything
  more belongs in `--packages`.
- **desktop** — a full graphical login. `--desktop gnome|kde|xfce|mate|
  cinnamon|lxqt` on Debian (the `task-*` metapackages), `gnome|kde|xfce|mate|
  lxqt` on Ubuntu (the flavour metas). NetworkManager comes along for laptops,
  and on Debian so does the wifi and graphics firmware laptops actually have —
  the images' sources already carried `non-free-firmware`.

What makes desktop actually work, rather than merely contain packages: the
desktop metas deliver the desktop itself through Recommends, so they get their
own apt run *with* recommends while the base system keeps
`--no-install-recommends`; the default target is forced to graphical rather
than trusting a postinst; `systemd-networkd` is disabled in favour of
NetworkManager so two DHCP clients never fight over one NIC; and the
root-slot floor rises to 10 GiB for the profile, so a slot too small for a
desktop tree is corrected up front instead of failing an hour into dpkg.

An impossible combination — cinnamon on Ubuntu, a typo, `--desktop` without
the desktop profile — is refused before any work happens, on both surfaces:
the web UI answers 400 naming the environments that ARE available for the
distro, and `build-image.sh` dies at argument-parse time, proven by a new
unprivileged test whose every case must leave its working directory empty.

**The boot suite can finally finish.** The first run to get past slot
stability died of a full disk: every test parks an ~8 GB scratch disk in
`output/` and the suite now runs enough of them for the sum to matter — the
runner could not write even its own diagnostic log. Each harness now removes
its scratch disk on exit (the logs are the evidence; the disks were never
uploaded), and the boot job starts by returning the ~30 GB of preinstalled
toolchains the stock runner donates.

## v0.7.0 — 2026-08-16

**The project is now Flipside** (formerly `debian-ab-images` — old URLs
redirect). The old name undersold three of the product's four halves and read
as a file collection rather than a system; the new one names the mechanism —
a healthy machine keeps its side, and a bad update boots the flip side.
Deliberately unchanged, the way any rename that respects deployed machines
must be: the `debian-ab-builder` image tags, the `ab-*` script names, the
`dab-` container prefix, and the `LUKS_KV_PREFIX` default (existing recovery
passphrases live under that path in the secrets manager).

**The update signing key is no longer served to the imaging segment.** `output/`
is both the web UI's working directory and the HTTP root's `/images`, and the
provisioning listener served it with a directory listing and no deny rules — so
the RAUC signing private key (`output/rauc-keys/key.pem`), the secrets-manager
token (`.secrets-store.json`), the fleet record and the assignment table were
all one unauthenticated GET away from anything on the imaging network. The key
is the sharp one: whoever holds it signs bundles the whole fleet installs, and
the certificate baked into every image cannot be rotated without re-imaging.

The listener now denies `rauc-keys/`, dotfiles, the fleet record and the
assignment table, and the listing is off — everything a machine fetches is
named by an iPXE script or an assignment, never enumerated. The route test
mounts a fixture `output/` into the real container and asserts each secret
404s while images and bundles still serve; against the old configuration it
fails six ways.

**`make clean` no longer deletes the signing key.** It removed all of
`output/`, which included `rauc-keys/` — the key whose loss permanently orphans
every deployed machine, as UPDATES.md has always warned. `clean` now keeps
`rauc-keys/` and says so.

**The nightly boot tests can see inside encrypted images again — which is to
say, for the first time.** The nightly builds its test image with `--encrypt`,
but the slot-stability harness mounted raw partitions and matched filesystem
labels, both invisible inside a LUKS container. The probe was installed into
zero slots — the harness said so, in an empty list, and carried on — and every
boot then ran probeless to its ten-minute timeout. Every scheduled nightly
since the test landed failed this way; the push/PR job never runs the boot
suite, so nothing caught it. The harness now opens the containers the way the
machine itself does (the keyfile on the plaintext BOOT partition, then the
build passphrase), and the two silent no-ops — a probe installed nowhere, a
rollback staged into a rootfs-b that was never found — are hard failures at
the cause rather than three boots later.

**Smaller hardening, found in the same audit:**

- The build command quoted every argument except the output name, and the
  default name was built from unsanitised `distro`/`suite`. Admin-only surface,
  but now sanitised and quoted like everything else, with a test.
- The CI workflow token is `contents: read` — nothing in it writes.
- `starlette` is pinned exactly, like every other backend dependency, and
  Dependabot now watches pip, npm, the workflow actions and all five
  Dockerfiles weekly.

**The docs now match the code.** The required `INTERFACE` variable is in the
first-run instructions (the server refuses to start without it); `dhcp` is
documented as the default mode it actually is; the slot-selection explanation
describes the v0.6.0 PROVEN/TRY mechanism instead of the one it replaced; the
imager-options table gained `imager.hostname=` and `imager.report=`; and there
is a "Backing up the server" section that names the files worth keeping —
starting with `rauc-keys/`.

**Smaller usability work:** `make bundle` builds a signed update bundle without
knowing the docker incantation; `make imager ARCH=arm64` works; `./builder/run.sh
--help` prints help without building a container first; both entry scripts check
for a working Docker daemon before doing anything; the README carries a CI badge;
and reporting a vulnerability now has a real channel (GitHub private
vulnerability reporting) instead of "contact the maintainer".

**The imager initramfs is a quarter of the size it was.** On amd64 it unpacks to
**113 MB instead of 502 MB**, and the download dropped from 123.5 MB to 97.3 MB.
Peak memory to boot it — the compressed image plus the unpacked tree, both held at
once — falls from roughly 628 MB to about 211 MB.

Almost all of it was the module tree. Debian ships modules as `.ko.xz`, busybox's
`modprobe` cannot read a compressed module, so the build expanded all 5188 of them
in place: 504 MB of the old 527 MB. The real `modprobe` from kmod reads them
compressed, so it is shipped instead and the tree is left exactly as Debian
packaged it. The download shrank as well, because gzip was being asked to compress
data that xz had already compressed better.

Two things this changed that are worth knowing:

- **kmod dlopens its decompressors.** `liblzma.so.5` and `libzstd.so.1` are not in
  the binary's `NEEDED` list, so `ldd` never reports them and the helper that
  copies a binary's libraries cannot see them. They arrive today only because the
  `zstd` binary happens to link liblzma — an accident, not a dependency. They are
  now copied deliberately, and the build proves a real `.ko.xz` can be decompressed
  by the shipped binary with the shipped libraries, chrooted so nothing on the
  build host can stand in for something the initramfs is missing.
- **`depmod` now has to succeed.** `modprobe` resolves dependencies purely from
  `modules.dep`, and the initramfs no longer regenerates it at boot — busybox's
  `depmod` cannot read compressed modules, so running it would have replaced a
  correct index with an empty one.

**The kernel and the initramfs are published together.** The kernel was copied to
its served path at the start of the build, minutes before the initramfs it belongs
with was finished. A build that failed in between left a new `vmlinuz` beside the
previous `initramfs.img`, and across a kernel bump that is a machine booting a
kernel whose modules it does not have — no network, no storage, and nothing that
says why. Both are now staged and renamed into place at the end, after the
initramfs has been verified.

**A machine netbooting during an imager rebuild no longer boots a half-written
imager.** The pack wrote the initramfs with a redirect straight onto
`output/imager/initramfs.img` — the file the PXE server is serving. The redirect
truncates it the moment packing starts and then fills it over the several minutes
the module tree takes, so a machine that netbooted in that window fetched a
complete kernel and a partial archive, found no `/init` in it, and stopped at:

```
Run /sbin/init as init process
Run /etc/init as init process
Run /bin/init as init process
Run /bin/sh as init process
Kernel panic - not syncing: No working init found.
```

Nothing in the build output said anything, and by the time anyone inspected the
file the build had finished and it was whole again. Note the panic carries no
"Failed to execute /init" line — the kernel prints that only when `/init` exists
and cannot be run, and says nothing at all when it is simply absent — so the
message names neither the archive nor the file.

The kernel and the initramfs are now packed beside their destinations and moved
into place. A rename within one filesystem is atomic, so a machine gets either
the previous imager or the new one, never part of either. The same property
covers a build that dies partway: it no longer destroys the working imager it
was replacing.

**The imager is checked before it is published.** `imager/verify-initramfs.sh`
confirms the archive is a complete gzip stream and that `/init` and
`/bin/busybox` (the interpreter its shebang names) are both in it. The build runs
it and refuses to publish otherwise, and it can be run by hand against a server's
live artifact — which is the quickest way to tell a broken imager from a broken
image when a machine panics on netboot. cpio's stderr is no longer discarded.

**A per-machine iPXE script now boots the imager the machine can run.** The
generated per-machine script had drifted from the fallback template: it hardcoded
`/imager/` rather than choosing from iPXE's `${buildarch}`, so an arm64 machine
*with an assignment* was handed the amd64 imager while the same machine left
unassigned booted correctly. Neither fetch was guarded, so a missing imager
aborted the script instead of saying so and rebooting. Both now match the
fallback.

**Whole folders can be uploaded to the image files.** *Choose a folder…* on the
Image Files page takes everything beneath it and keeps the tree. Previously the
only way to ship a directory was one file at a time, or a shell on the host —
the gap that page exists to close.

You pick the destination and whether the folder's own name forms part of the
path (`etc/` → `/etc/hosts`, or strip the wrapper and land its contents
directly), and **the exact list of destinations is shown before anything is
sent** — that mapping is the part that is easy to get wrong, and a folder
landing one directory off is discovered by a machine behaving oddly rather than
by an error.

Each file goes through the same upload endpoint a single file does, so
everything that keeps a path inside `overlay.d` is unchanged; a bulk endpoint
would have been a second way in to the one place in this app where a browser's
path reaches `open()`. Failures are named per file rather than counted.

`.DS_Store`, `Thumbs.db`, `.gitkeep` and `.git/` trees are skipped and *counted
in the preview* rather than quietly dropped. There are limits on file count and
total size, because a mis-picked home directory is a real hazard. Empty
directories are not uploaded: a browser does not report them and the builder
copies files.

A browser cannot read file permissions, so uploads arrive `0644` unless the path
is one that is usually executable. The panel says so — a folder of scripts that
silently does not run is the failure this avoids.

## v0.6.0 — 2026-08-09

Two features, and a chain of imaging bugs that one bug report pulled apart.

"The hostname field on the Provisioning page does nothing" turned out to sit on
top of three defects that had been there far longer and were concealing each
other: the imager could never mount ext4, so it never wrote the marker the
installed system reads; every machine identified itself as `unknown-1`; and the
progress reports had no route to the web UI. None of them announced itself,
because each one's symptom was the next one's silence.

Machines are also no longer one bad reboot away from switching slots — a
separate hunt, same shape: a boot counter that was armed on every boot and
disarmed too late to matter.


**Fixed: a killed build poisoned every build after it.** `Device luks-rootfs-a
already exists.` The builder opened its LUKS volumes under the same mapper names
the *installed system* uses, so a build that died before its cleanup trap ran
left those mappings in the kernel and every later encrypted build failed on the
first one.

Worse than an annoyance: building an image on a machine that is itself an A/B
system would have collided with that machine's own live root mapping, and the
cleanup would then have closed it.

Build-time mappings are now `abbuild-<random>-{a,b,ovl}`, unique per build and
distinct from the installed system's names, which stay fixed in `crypttab` and
`rauc/system.conf` where they have to be stable. Random rather than `$$`:
containers share the host's device-mapper namespace and two concurrent builds
are quite likely to both be PID 7.

Clearing a stale mapping by hand, if one is left over from an older build:

```bash
for m in luks-rootfs-a luks-rootfs-b luks-overlay; do
  sudo cryptsetup close "$m" 2>/dev/null || true
done
```

**Fixed: every machine reported itself as `unknown-1`.** The imager derives its
identity from the MAC of the interface that took the DHCP lease — that is what
the DHCP server and the iPXE menu know the machine by, so the UI can join a
machine's imaging progress to its later check-in without a lookup table.

It was computed at the top of `init`, *before* any network driver had been
modprobed. The only interface in existence at that point is `lo`, whose address
is all zeroes and is filtered out, so the lookup found nothing and fell through
to `unknown-$$` — and since `/init` is PID 1, that is `unknown-1` on every
machine. A whole fleet would have reported under one identity, collapsing into a
single row in the UI and overwriting each other.

Nothing showed it because neither consumer worked: the reports 404'd and the
check-in marker was never written. Fixing those is what would have made a fleet
of machines all appear as one.

The identity is now resolved after the network is up, preferring the interface
that actually took a lease. If no MAC can be found at all it still falls back,
but says so on the console rather than letting machines quietly share an id. The
imager prints `Reporting as <mac>`, and `test-imager-e2e.sh` asserts the id in
the marker is a MAC.

**Fixed: the imager could never mount ext4, so no machine has ever left a
check-in marker.** `EXT4-fs: Cannot load crc32c driver.` ext4 uses crc32c for
metadata checksums and requests it at *runtime* through the crypto API, not as a
link-time symbol — so it never appears in `modules.dep`, `depmod` never resolves
it, and `modprobe ext4` never pulled it in. The module loaded and registered, so
`ext4` appeared in `/proc/filesystems` and everything looked correct; the mount
then failed, and busybox reported it as `ENOENT` — "No such file or directory",
naming a device and a directory that both existed and were both readable.

Consequences, all silent:

- `/boot/ab-deploy.json` was never written on any machine ever imaged.
- `ab-checkin.service` has `ConditionPathExists` on that file, so it never ran —
  no machine has ever reported that it booted, and the Fleet page could only
  ever show `never-booted`.
- The per-machine hostname added earlier in this release travelled as far as the
  imager and stopped there, so assigned names were silently ignored.

`crc32c_generic` and `libcrc32c` are now loaded. Alongside that, three things
that turned a one-line kernel message into a long hunt:

- `leave_checkin_marker` returned 0 on every failure path without a word. It now
  says which step failed and prints the kernel log, which named the cause
  immediately the first time it was asked directly.
- The BOOT partition's device node is waited for and, failing that, created from
  `/sys/class/block` — there is no udev in the initramfs and the old code tested
  for the node on the line after `blockdev --rereadpt`.
- `blockdev --rereadpt` no longer discards its exit status.

`scripts/test-imager-e2e.sh` covered this hop and **was never wired into CI**. It
now runs nightly, passes `imager.hostname=` and asserts both the marker and that
the machine boots under the assigned name — against an image deliberately built
with a different one. It is also arch-aware now, so it can be run on an
Apple-silicon host instead of only after a mistake has shipped.

**The imager states what it supports.** `output/imager/build.json` records the
build time and the `imager.*` parameters the built `init` understands, derived
from `init` itself. The Provisioning page reads it back and warns when an
assignment needs a parameter the built imager lacks — the imager is a separate
artifact, and `git pull` does not rebuild it.

**Machines can be named in the provisioning assignment.** An image cannot carry
a hostname — every machine written from it would answer to the same one — so the
only way to name a machine was to log in after imaging and set it by hand. That
is manual per-machine state on a fleet whose premise is that machines are
interchangeable, and on this project it was also the sequence that walked an
operator into the boot-counter bug fixed earlier in this release.

The assignment gains a **Hostname** field next to the existing (cosmetic) label.
It travels assignment → `/hosts/<mac>.ipxe` → `imager.hostname=` → the imager →
`/boot/ab-deploy.json` → `machine-identity.service`, so nothing is configured on
the machine and nobody logs in afterwards.

It is stored with the machine-id and SSH host keys on the overlay partition
rather than only in `/etc/hostname`: `/etc` is part of the root, so an image
could ship a file that shadows it, and under `--slot-private-upper` a name set
in slot A would not exist in slot B. Re-applied every boot, so it is right in
both slots and survives updates. `/etc/hosts` gets the matching `127.0.1.1`
entry, without which `sudo` waits on a resolution timeout.

Names are refused rather than sanitised — `web 01` would split into a second
kernel parameter, and a name in the UI that differs from the name on the machine
is found by whoever cannot resolve it. Duplicates are refused
case-insensitively. Blank keeps the image's own name, so nothing changes for
anyone not using it.

**Health checks decide whether an update is kept.** Drop an executable in
`/etc/ab/health.d/` (via `overlay.d`) and it runs before logins are permitted;
if any check fails the slot is never marked good and the next boot rolls back to
the previous release. With no checks installed nothing changes — booting far
enough to permit logins is still the test, which is what it has always been.

This is systemd's own boot-assessment shape: checks are ordered before
`boot-complete.target`, and `ab-mark-good` `Requires=` that target. Checks run
before `systemd-user-sessions.service` so adding them does not reopen the window
that let a reboot switch slots, with a 60s timeout so a hung check cannot
produce a machine nobody can log into. A failed check does not block login — the
machine comes up so you can see why, it simply is not blessed.

The first version of this was wired `WantedBy=boot-complete.target`, which is a
`Wants=` — a failing check left the target `active`, the slot was blessed anyway
and nothing rolled back. The mechanism was decorative, and only a real boot
showed it (`health-check=failed`, `boot-complete=active`,
`mark-good-result=success`, `B -> B -> B`). It is `RequiredBy=` now, and
`test-slot-stability.sh` gained a `health-fail` mode that installs a failing
check and asserts the machine rolls back (`B -> A -> A`).

**Fixed: `test-luks-key-portability.sh` failed intermittently on unrelated
changes.** `losetup -P` scans partitions the moment they appear — before `mkfs`
writes a label — so blkid's cache could hold a label-less entry for the very
device the test had just labelled. `blkid -t LABEL=BOOT` then found nothing,
`ab-luks-key` correctly warned that it could not find the BOOT partition, and
the test recorded two failures that looked like the product was broken when
nothing about it had changed. It is the same trap `test-state-directives.sh`
documents for `blkid -L overlay`.

The cache is now dropped before each invocation, and the harness asserts up
front that the label resolves to the device it just made — so a recurrence is
reported as `HARNESS-FAIL` rather than as a product failure. `ab-luks-key` also
gained a cache-bypassing `blkid -c /dev/null` retry, since a scan before a label
is written is possible on a real machine too.

**Fixed: machines being imaged never appeared on the Imaging page.** A machine
would show `booting imager` on the Provisioning page, image perfectly, boot —
and never show up on the Imaging page at all, with no error anywhere.

The imager posts progress to `<image host>/api/imaging/report`. That is the only
address it has: the iPXE scripts pass `imager.url=` and nothing else, so `init`
derives the report URL from the host serving the image. That host is the
provisioning nginx, whose config had exactly three locations — `/images/jobs/`,
`/`, `/health` — and no `/api/` at all. Every report was a POST into the static
file root, answered 404 and discarded. The web UI runs as a separate stack on
port 8080 and nothing bridged the two.

Reporting is best-effort by design, so `report()` ended in `|| true` and said
nothing. That silence is why this could sit there looking like a UI bug.

- The provisioning nginx now proxies **exactly** `/api/imaging/report` and
  `/api/imaging/checkin` to the web UI, configured by `WEBUI_ADDR` in
  `server/.env` (default `127.0.0.1:8080`, right when both stacks share a host).
- Exact-match locations, not a prefix over `/api/`. The rest of the API is the
  admin surface, and although it is all behind `require_auth`, none of it should
  be reachable from the imaging segment. `/api/imaging` (the list) and
  `/api/imaging/<id>` (delete) are deliberately not routed either.
- A `WEBUI_ADDR` that does not resolve no longer takes PXE down with it: nginx
  refuses to start on an unresolvable `proxy_pass` name, so the entrypoint runs
  `nginx -t`, falls back to the default and says so. Losing the progress display
  is survivable; losing PXE is not.
- The imager now prints a note on the console, once per run, when it cannot
  reach the report URL — imaging still continues, but the failure is no longer
  invisible.

This also fixes first-boot check-in, which used the same derived URL: the
`checkin_url` the imager leaves in `/boot/ab-deploy.json` pointed at the same
dead route, so `ab-checkin` could never reach the server either. Note a machine
that has already moved to its production network still cannot reach
`SERVER_IP`; that is the same limitation `UPDATE_IP` exists for.

`scripts/test-imaging-report-route.sh` runs the real container against a stub
web UI and asserts both that the two endpoints arrive and that nothing else
does. Without the fix it reports `POST /api/imaging/report -> 404`.

**The A/B fallback is now armed for updates, not for every boot.** A slot carries
`<SLOT>_PROVEN` in grubenv; GRUB boots a proven slot outright and only puts an
**unproven** one on probation. `ab-slot-pending.sh` sets `_PROVEN=0` on the slot
an update has just written, and `ab-mark-good` sets it back once that slot boots.

Previously the counter was armed on *every* boot, so every boot for the life of
the machine had to be blessed before the next reboot, and any failure to do so
switched slots. That is the mechanism behind the bug below; this removes it
rather than narrowing it. It is also what Android
(`successful`/`tries_remaining`), ChromeOS (`cgpt successful`/`tries`) and
systemd-boot (`entry+N-M.conf`) do — RAUC's reference GRUB integration, which
this project followed, arms every boot. Between updates nothing writes to `/boot`
at all now.

`ab-slot-pending.sh` is wired as RAUC's `[handlers] post-install`, not into
`ab-update`, so `rauc install <bundle>` typed by hand — which `make-bundle.sh`
prints and the web UI shows — gets the same protection. `ab-update` calls it
again as belt and braces, since a handler that did not run means an update with
no rollback.

The risk in this change is the inverse of the bug: arm too little and rollback
quietly disappears, discoverable only when needed. `test-slot-stability.sh`
gained a `rollback` mode that stages what an update stages, makes the new slot
fail to mark good, and asserts the machine falls back (`B -> A -> A`). Both ends
run nightly.

**Fixed: a machine could switch slots by itself after a perfectly good boot.**
Image a machine, log in, change something, reboot — and it comes up on the other
slot with none of your changes. It reads as the machine reverting itself. The
report that found this was "boot the image, `hostnamectl set-hostname`, reboot,
and it boots slot B".

`grub.cfg` arms a one-shot fallback on every boot (`<SLOT>_TRY=1`) and
`ab-mark-good` disarms it once the system is up. It was ordered
`After=multi-user.target`, and on a measured machine that meant it ran **~92
seconds** into boot while the login prompt appeared at **~38 seconds** — a ~50
second window where the machine was fully usable and the counter was still
armed. Reboot inside it and GRUB does exactly what it was told: skip this slot.
Nothing about the machine looked wrong, because nothing was: the fallback fired
as designed, far too late to be disarmed by a unit nobody was waiting for.

Three fixes:

- **`ab-mark-good` now runs before logins are permitted**
  (`Before=systemd-user-sessions.service`) rather than after
  `multi-user.target`. The rule is now "if you can log in, this slot is already
  marked good". The trade is deliberate and narrow: a boot that reaches a login
  prompt and then fails a later service no longer rolls back on its own.
  Everything A/B rollback actually exists for — an unbootable kernel, a broken
  initramfs, a root that will not mount, a drop to emergency — happens before
  that point and is still covered.
- **An ordering cycle is gone.** `ab-mark-good` was `After=multi-user.target`
  *and* `WantedBy=multi-user.target`, while `ab-checkin` was `After=` it and also
  `WantedBy=multi-user.target`. systemd broke the loop by deleting a job at every
  boot (`Found ordering cycle on multi-user.target/start`). It chose `ab-checkin`
  on the boots that were watched, but both units are only *Wanted*, so both were
  eligible — and deleting `ab-mark-good` means the counter is never reset at all.
  `ab-checkin` no longer orders itself after `ab-mark-good`; it only reports, so
  the edge bought nothing.
- **Failures are no longer silent.** `ab-mark-good.sh` exited 0 on every failure
  path, so `systemctl status ab-mark-good` said "success" on a machine whose
  counter was still armed — the one place anyone would look actively said
  nothing was wrong. It now exits non-zero, says the counter is still armed, and
  reads the value back after writing rather than trusting the write.

`scripts/test-slot-stability.sh` is the regression test, in the nightly boot
matrix: boot a machine three times and assert it never changes slot on its own.
Its `early-reboot` mode reboots 45 seconds in, which is what a person does and
what used to fail. Nothing covered this before — the only related check was a
soft `WARN` in the update test — so a machine that alternated slots for the rest
of its life would have shipped green.

**Bundles can be deleted from the web UI.** There was no way to remove one short
of reaching into `output/bundles/` on the server, so the list only ever grew —
at roughly half a gigabyte per bundle.

The delete is not just an `rm`, because `bundles/latest` exists: `ab-update` with
no arguments fetches that pointer and installs whatever it names, and directory
listing is off on the HTTP server, so it is the only way an unattended machine
finds a bundle at all. Deleting the file it named would have broken every
unattended machine at once, reported as a download failure or `is not a RAUC
bundle` rather than as something missing on the server. Deleting now moves the
pointer to the newest bundle left, or removes it when the last one goes.

The Updates page marks which row is **latest** and how many machines report each
version, so both consequences are visible before the confirm. Deleting a version
the fleet is running is safe — the update is on their disks and rollback uses the
other slot — but it does prevent installing that version anywhere else, which the
confirm says. Deletion is refused while a bundle build is running, since that
build rewrites the same pointer.

**`--slot-private-upper`: each slot can have its own overlay upper layer.** A/B
protected a machine from a bad image but not from a bad change. Both slots share
one upper layer, so a broken `/etc/fstab` or a bad `systemd-networkd` file stops
slot A booting *and* slot B, which reads the same layer — and "boot the other
slot" is the first thing anyone tries.

Built with `--slot-private-upper`, the overlay partition carries `upper-A` and
`upper-B` instead of one `upper`. Nothing written while running A is visible from
B, so the other slot really is a fallback. Not the default: the slots then share
nothing the overlay covers, so pair it with `--persist /home` for what should
survive the crossing. Machine identity (machine-id, SSH host keys) lives outside
the upper layer and stays shared either way.

Like the state model, it cannot be turned on or off by an update — the machine
records `overlay+per-slot-upper` in `/var/lib/overlay/.model` and refuses an
image declaring the other layout, because every write it has made is in the store
the old one used. The recovery *reset writable state* entry sets aside only the
booted slot's layer, and `ab-overlay-diff` reads that slot's layer and stops
claiming the files it lists shadow the image on both slots.

Available as `SLOT_PRIVATE_UPPER=1` to `make image` and as a checkbox under
**Writable state** in the web UI. See
[BUILDER.md](docs/BUILDER.md#a-separate-upper-layer-per-slot).

## v0.5.1 — 2026-08-08

**Two images of the same kind can coexist.** The output name was
`{distro}-{suite}-{arch}-ab` and nothing else, so a second Debian 13 amd64 build
silently replaced the first — and with it the image a deployed machine was made
from (which is what a bundle for that machine has to be built from), its
sidecars, and **the LUKS passphrase**, which the secrets manager files under the
image name. An unrelated later build destroyed the recovery key of a machine
already in the field, discoverable at the earliest by whoever needed it at a
console.

Builds are named now. Left blank, a free name is chosen (`…-ab`, then `-2`,
`-3`), so the default cannot overwrite anything. Give a name and it is honoured,
and refused if taken — with a free alternative named in the refusal, which the
UI offers and fills in. Tick **Replace if it exists** to mean it.

## v0.5.0 — 2026-08-08

**Over-the-air updates work on encrypted machines, and reach machines that have
left the provisioning network.** Every fix here came out of one bundle that
would not install; four separate causes, none of which reported itself
accurately.

### Encrypted updates

Encrypted machines could never be updated. Two independent bugs, both the same
mistake — an image carrying something that belongs to one disk:

- **crypttab named the build's own LUKS UUIDs.** `cryptsetup luksUUID` returns a
  value created by that `luksFormat`, so the crypttab described the builder's
  loopback file. A bundle carries the rootfs *and* the initramfs built from it,
  so a machine given a bundle from another build hunted for three volumes that
  exist nowhere on its disk and waited forever
  (`Waiting for encrypted source device UUID=…`). Volumes are now addressed by
  `PARTLABEL=`, which comes from the GPT and is identical in every build.
- **The LUKS bootstrap keyfile was baked into the image**, and therefore into
  the initramfs a bundle delivers — so an update handed the machine the
  *builder's* key (`No key available with this passphrase`, then an initramfs
  shell). The key now lives on the BOOT partition, which no bundle writes, and
  `scripts/init-premount/ab-luks-key` fetches it at boot.

If **all three** volumes fail to resolve, that is provenance, not a damaged
disk. One failing volume is hardware.

Machines imaged before this need a one-time crypttab repair from the other slot;
the procedure is in [docs/UPDATES.md](docs/UPDATES.md). Re-imaging is simpler
where it is available.

### Security

- **Update bundles no longer contain LUKS key material.** `make-bundle.sh` tars
  the whole root slot with no exclusions, so every bundle built from an
  encrypted image carried that image's keyfile — and bundles are published over
  plain HTTP for any machine to fetch.
- **An image with no signing certificate now ships an empty keyring** rather
  than `ca-certificates.crt`. The old fallback did not merely fail to help: it
  made the machine accept a bundle signed by anything chaining to any public CA.
- **The web UI no longer answers for paths it does not own.** Its SPA catch-all
  returned `index.html` with a 200 for any unknown path, so a bundle URL on the
  UI's port downloaded the React app under a `.raucb` name.

### Updates reach the fleet

- **`UPDATE_IP`** (optional, in `server/.env`) publishes `/bundles/` on a second
  address. The imaging listener is bound to `SERVER_IP` alone, but a machine is
  only on that segment while it is being imaged — afterwards it lives on the
  LAN, which is when it needs updates. `/images/`, `/imager/` and `/hosts/` stay
  where they were.
- **`build-image.sh` generates the signing key** if it does not exist, so an
  image and the bundles for it always agree. Previously an image built before
  the first bundle trusted nothing, and failed months later with
  `Verify error: self-signed certificate` — unrepairable remotely, because the
  update is the thing it will not accept.
- **`ab-update` checks the first four bytes are `hsqs`** before handing a URL to
  RAUC, so a wrong URL says so instead of surfacing as a corrupt bundle.
- Hand-set values in `server/.env` survive the Provisioning page saving.

### Streaming

`rauc install <url>` streams over NBD and had been assumed broken. It is not:
the only thing testing it served bundles with `busybox httpd`, which answers a
range wholly past EOF with `200` and the entire file where nginx answers `416`.
RAUC's NBD backend requires `206` with exactly the requested byte count. The
nightly now serves from nginx, and installs report `install-route: streamed`.
The download fallback stays — upstream streaming genuinely dies on flaky
networks — but it is a fallback again rather than the path every update takes.

### Tests

Both encrypted bugs were found by imaging a real machine, because the only thing
watching encrypted updates was a nightly that did not test them. Now:

- `test-luks-key-portability.sh` — real GPT, loop device and LUKS, the real
  init-premount script and hook, in the **fast** job. The volume it opens has no
  passphrase slot, so only the fetched key can open it.
- `test-cross-build-update.sh` — takes the initramfs out of a bundle, runs it
  against a *different* build's disk, and checks it unlocks. Asserts the two
  builds do not share a key, so it cannot pass vacuously.
- `test-image-portability.sh` — asserts a built image carries no LUKS UUID, no
  key material, and an initramfs that fetches rather than carries a key.
- The nightly update test runs twice, unencrypted and encrypted across two
  independent builds, and fails on `Waiting for encrypted source device` by name.
- Every built image's keyring fingerprint must match the signing certificate.

### Other

- Image sidecars record `update_keyring_sha256`.
- `builder/run.sh` warns when a build will not fit on the host. Docker Desktop's
  disk image is sparse and provisioned far above what the host can supply, so
  `df` inside the builder reports space that does not exist and overrunning
  corrupts Docker rather than failing with ENOSPC.

## v0.4.0 and earlier

See the git history; this file starts at v0.5.0.
