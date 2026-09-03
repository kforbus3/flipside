import { useEffect, useRef, useState } from "react";
import { Hammer, Cpu, XCircle, FolderPlus } from "lucide-react";
import { api, apiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useToast } from "../components/Toast";
import { Button, Card, Input, Label, Select, PageHeader, LogView, Badge, Alert, ProgressBar, Modal } from "../components/ui";
import OverlayManager from "../components/OverlayManager";

// One list, used for the Architecture selector and the imager buttons, so the
// two can never drift apart.
const ARCHES = [
  { value: "amd64", label: "x86_64" },
  { value: "arm64", label: "ARM64" },
];

const SUITES: Record<string, { value: string; label: string }[]> = {
  debian: [
    { value: "trixie", label: "trixie (13)" },
    { value: "bookworm", label: "bookworm (12)" },
  ],
  ubuntu: [
    { value: "resolute", label: "resolute (26.04 LTS)" },
    { value: "noble", label: "noble (24.04 LTS)" },
    { value: "jammy", label: "jammy (22.04 LTS)" },
  ],
};

// Mirrors the backend's mapping (app/orchestrator.py DESKTOP_ENVS): which
// desktop environments each distro can actually provide. The server refuses
// anything off this list with a 400, so the select only ever offers what will
// build — Ubuntu has no Cinnamon flavour, and showing it would turn a distro
// switch into an error message.
const DESKTOPS: Record<string, { value: string; label: string }[]> = {
  debian: [
    { value: "gnome", label: "GNOME" },
    { value: "kde", label: "KDE Plasma" },
    { value: "xfce", label: "Xfce" },
    { value: "mate", label: "MATE" },
    { value: "cinnamon", label: "Cinnamon" },
    { value: "lxqt", label: "LXQt" },
  ],
  ubuntu: [
    { value: "gnome", label: "GNOME (Ubuntu desktop)" },
    { value: "kde", label: "KDE Plasma" },
    { value: "xfce", label: "Xfce (Xubuntu)" },
    { value: "mate", label: "MATE (Ubuntu MATE)" },
    { value: "lxqt", label: "LXQt (Lubuntu)" },
  ],
};

// The builder raises a desktop build's root slot to this floor — a desktop
// tree is several GiB installed. Mirrored here so the size math below warns
// about the build that will actually happen.
const DESKTOP_MIN_ROOT = 10240;

export default function Build() {
  const toast = useToast();
  const { canOperate } = useAuth();
  const [opts, setOpts] = useState({
    distro: "debian", suite: "trixie", arch: "amd64",
    profile: "minimal", desktop: "gnome", secure_boot: "auto",
    name: "", replace: false,
    hostname: "debian-ab", username: "admin", password: "",
    image_size: 0, root_size: 3072, compress: "zstd", packages: "",
    ssh_key: "", ssh_key_only: false,
    encrypt: false, unlock: "keyfile", luks_passphrase: "", tang_url: "",
    store_passphrase: false,
    run_script: "", own_paths: "",
    state_model: "overlay", slot_private_upper: false,
    persist_paths: "", slot_private_paths: "",
    volatile_paths: "", reset_paths: "", keep_paths: "",
  });
  const [store, setStore] = useState<{ configured: boolean; provider: string } | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [jobId, setJobId] = useState<string>("");
  const [problems, setProblems] = useState<string[]>([]);
  const [progress, setProgress] = useState<{ step: number; total: number; label: string } | null>(null);
  const [imagerArches, setImagerArches] = useState<Record<string, boolean> | null>(null);
  const [overlay, setOverlay] = useState<{ files: { path: string; size: number }[]; dir: string } | null>(null);
  const [customOpen, setCustomOpen] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  const loadImagerArches = () =>
    api.get("/images").then((r) => setImagerArches(r.data.imager_arches || null)).catch(() => {});

  useEffect(() => () => esRef.current?.close(), []);
  useEffect(() => {
    api.get("/preflight").then((r) => setProblems(r.data.problems)).catch(() => {});
    loadImagerArches();
    api.get("/overlay").then((r) => setOverlay(r.data)).catch(() => {});
    api.get("/secrets/config")
      .then((r) => setStore({ configured: r.data.configured, provider: r.data.config.provider }))
      .catch(() => setStore({ configured: false, provider: "" }));
    // Builds run for many minutes, so navigating away or reloading must not lose
    // the live log — reattach to whatever build is still running. The stream
    // replays the job's backlog before going live, so nothing is missed.
    api.get("/jobs").then((r) => {
      const job = r.data.find((j: any) => j.status === "running" && (j.type === "image" || j.type === "imager"));
      if (job) stream(job.id);
    }).catch(() => {});
  }, []);

  async function stream(id: string) {
    setLog([]); setProgress(null); setRunning(true); setStatus("running"); setJobId(id);
    // Streams are authorized by a short-lived per-job token, not the session JWT.
    const { data } = await api.get(`/jobs/${id}/stream-token`);
    const es = new EventSource(`/api/jobs/${id}/stream?token=${data.token}`);
    esRef.current = es;
    es.onmessage = (e) => setLog((l) => [...l, e.data]);
    es.addEventListener("progress", (e: any) => { try { setProgress(JSON.parse(e.data)); } catch {} });
    es.addEventListener("end", (e: any) => {
      es.close(); setRunning(false); setStatus(e.data);
      e.data === "success" ? toast.success("Build finished") : toast.error(`Build ${e.data}`);
    });
    es.onerror = () => { es.close(); setRunning(false); };
  }

  async function startImage() {
    try {
      // The desktop field only means anything with the desktop profile, and
      // the server refuses the pair rather than guessing — so it is not sent
      // at all for the other profiles, where it is just the select's memory.
      const { desktop, ...rest } = opts;
      const payload = opts.profile === "desktop" ? { ...rest, desktop } : rest;
      const { data } = await api.post("/builds", payload);
      // Said once, at the moment it becomes true. The passphrase is written
      // before the build starts, so this is already a fact rather than a plan.
      if (data.passphrase_stored_at) toast.success(`Passphrase stored at ${data.passphrase_stored_at}`);
      // Which name it actually got. With no name given a free one is chosen
      // rather than replacing what is there, so this is not always the obvious
      // one -- and the Updates page will list it under exactly this.
      if (data.image_name) toast.success(`Building ${data.image_name}`);
      await stream(data.id);
    } catch (e) {
      // A name collision is refused rather than allowed to overwrite, and the
      // server names a free alternative. Offer it instead of making the
      // operator invent one.
      const d = (e as any)?.response?.data?.detail;
      if (d && typeof d === "object" && d.suggestion) {
        toast.error(`${d.detail} Try "${d.suggestion}", or tick Replace to build over it.`);
        setOpts((o) => ({ ...o, name: d.suggestion.replace(/\.img$/, "") }));
        return;
      }
      toast.error(apiError(e));
    }
  }
  // The imager is built for the architecture selected above, because it is a
  // kernel the target machine runs: an amd64 imager cannot netboot an arm64
  // machine, so building an arm64 image without one leaves it undeployable.
  async function startImager(arch: string) {
    try {
      const { data } = await api.post("/imager/build", { arch });
      await stream(data.id);
      loadImagerArches();
    } catch (e) { toast.error(apiError(e)); }
  }
  async function cancel() {
    try { await api.post(`/jobs/${jobId}/cancel`); toast.success("Cancel requested"); }
    catch (e) { toast.error(apiError(e)); }
  }
  const set = (k: string, v: any) => setOpts((o) => ({ ...o, [k]: v }));
  // Generating and storing is the right default wherever the passphrase is only
  // ever a recovery key — which is every unlock method except the one that
  // prompts for it at every boot.
  const generatable = (unlock: string) => !!store?.configured && unlock !== "passphrase";
  const setEncrypt = (on: boolean) =>
    setOpts((o) => ({ ...o, encrypt: on, store_passphrase: on && generatable(o.unlock) }));
  const setUnlock = (unlock: string) =>
    setOpts((o) => ({ ...o, unlock, store_passphrase: generatable(unlock) }));
  // The builder raises the root slot to a per-distro floor (Ubuntu's kernel
  // hard-depends on linux-firmware + linux-modules-extra, ~1.7 GiB Debian never
  // installs). Mirror that here so the size warning and the note below reflect
  // what will actually be built rather than what was typed.
  const MIN_ROOT: Record<string, number> = { ubuntu: 5120, debian: 2560 };
  const minRoot = Math.max(MIN_ROOT[opts.distro] ?? 2560,
                           opts.profile === "desktop" ? DESKTOP_MIN_ROOT : 0);
  const effRoot = Math.max(+opts.root_size || 0, minRoot);
  const rootRaised = effRoot > (+opts.root_size || 0);
  const neededMiB = 2 * effRoot + 512 + 128 + 2 + 256;
  // 0 = auto: the builder picks the smallest size (it expands on first boot).
  const sizeTooSmall = +opts.image_size > 0 && +opts.image_size * 1024 < neededMiB;
  const setDistro = (d: string) => setOpts((o) => ({
    ...o, distro: d, suite: SUITES[d][0].value,
    hostname: o.hostname === `${o.distro}-ab` ? `${d}-ab` : o.hostname,
    // A desktop the new distro does not package would be refused server-side;
    // fall back to GNOME, which both provide, rather than carrying a stale pick.
    desktop: DESKTOPS[d].some((x) => x.value === o.desktop) ? o.desktop : "gnome",
  }));

  return (
    <div>
      {/* Both architectures are listed whether or not either is built. A single
          button following the Architecture selector further down the form read
          as "this app only does amd64", because that is the default and nothing
          on screen suggested otherwise. */}
      <PageHeader title="Build Image" subtitle="Produce a bootable Debian or Ubuntu A/B image" actions={
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-500">Netboot imager:</span>
          {ARCHES.map((a) => {
            const built = imagerArches?.[a.value];
            return (
              <Button key={a.value} variant="secondary" size="sm" disabled={running || !canOperate}
                      onClick={() => startImager(a.value)}
                      title={!canOperate ? "Your viewer role is read-only" : built ? `Rebuild the ${a.label} imager` : `Build the ${a.label} imager`}>
                <Cpu size={13} />
                {a.label}
                <span className={built ? "text-emerald-400" : "text-zinc-500"}>
                  {built ? "built" : "not built"}
                </span>
              </Button>
            );
          })}
        </div>
      } />
      {/* An image is undeployable without an imager of the same architecture,
          and the only symptom is a machine that PXE-boots into nothing. */}
      {imagerArches && !imagerArches[opts.arch] && (
        <Alert title={`No ${opts.arch} netboot imager has been built`} items={[
          `Machines cannot be imaged over the network for ${opts.arch} until one exists — ` +
          `the imager is a kernel the machine itself runs, so it has to match. ` +
          `Build it with the ${opts.arch} button above; it can be built on this server ` +
          `whatever architecture the server itself is. Images built here can still be ` +
          `written to a disk directly in the meantime.`,
        ]} />
      )}
      <Alert title="Builds cannot run yet" items={problems} />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card className="p-5">
          {/* items-end keeps the inputs on a row level with each other when one
              label wraps to a second line and its neighbour does not. */}
          <div className="grid grid-cols-2 items-end gap-3">
            <div><Label>Distribution</Label><Select value={opts.distro} onChange={(e) => setDistro(e.target.value)}><option value="debian">Debian</option><option value="ubuntu">Ubuntu</option></Select></div>
            <div><Label>Release</Label><Select value={opts.suite} onChange={(e) => set("suite", e.target.value)}>{SUITES[opts.distro].map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}</Select></div>
            <div>
              <Label>Architecture</Label>
              <Select value={opts.arch} onChange={(e) => set("arch", e.target.value)}>
                {ARCHES.map((a) => (
                  <option key={a.value} value={a.value}>{a.label} ({a.value})</option>
                ))}
              </Select>
            </div>
            <div><Label>Compression</Label><Select value={opts.compress} onChange={(e) => set("compress", e.target.value)}><option value="zstd">zstd</option><option value="gzip">gzip</option><option value="none">none</option></Select></div>
            {/* What the image is for, as a named package set. Minimal is
                exactly the base system this builder has always produced —
                choosing it changes nothing — so the default stays honest for
                existing users while server/desktop are visible choices rather
                than package lists everyone retypes. */}
            <div>
              <Label>Profile</Label>
              <Select value={opts.profile} onChange={(e) => set("profile", e.target.value)}>
                <option value="minimal">Minimal (base system)</option>
                <option value="server">Server (headless tools)</option>
                <option value="desktop">Desktop (graphical login)</option>
              </Select>
            </div>
            {opts.profile === "desktop" && (
              <div>
                <Label>Desktop environment</Label>
                {/* Options depend on the distro above: the backend refuses a
                    combination the distro does not package, so nothing
                    unbuildable is offered here. */}
                <Select value={opts.desktop} onChange={(e) => set("desktop", e.target.value)}>
                  {DESKTOPS[opts.distro].map((d) => (
                    <option key={d.value} value={d.value}>{d.label}</option>
                  ))}
                </Select>
              </div>
            )}
            <div>
              <Label>Secure Boot</Label>
              <Select value={opts.secure_boot} onChange={(e) => set("secure_boot", e.target.value)}>
                <option value="auto">Auto — use signed shim and GRUB if available</option>
                <option value="on">Required — fail the build without them</option>
                <option value="off">Off — unsigned GRUB only</option>
              </Select>
            </div>
            {opts.secure_boot !== "off" && (
              <p className="col-span-2 text-xs text-zinc-500">
                The image carries the distribution's signed shim and GRUB, so it boots
                on machines where Secure Boot is enforced — nothing of yours is signed
                and nothing needs enrolling. It also boots with Secure Boot disabled,
                so this costs nothing either way.{" "}
                <span className="text-zinc-400">
                  Imaging itself still needs Secure Boot off: the netboot imager is an
                  unsigned initramfs. Disable it, image the machine, turn it back on.
                </span>
              </p>
            )}
            {opts.secure_boot === "on" && (
              <p className="col-span-2 text-xs text-amber-300/80">
                The build fails rather than producing an unsigned image — worth using
                in a pipeline, so a suite that stops shipping signed shim packages
                breaks the build instead of quietly shipping something the fleet
                cannot run.
              </p>
            )}
            {opts.profile === "server" && (
              <p className="col-span-2 text-xs text-zinc-500">
                Adds a small headless-admin set on top of the base system: rsync,
                htop, less, nano, tmux. SSH and curl are already in every image.
              </p>
            )}
            {opts.profile === "desktop" && (
              <p className="col-span-2 text-xs text-zinc-500">
                Installs a full graphical environment with display manager and
                NetworkManager{opts.distro === "debian" ? ", plus common wifi firmware for laptops" : ""}.
                The desktop metapackage pulls a large dependency tree — the root
                slot is raised to {DESKTOP_MIN_ROOT} MiB and the build takes
                considerably longer.
              </p>
            )}
            {/* Fields are grouped rather than left to flow in source order: the
                two-column grid had put Username and Password diagonally opposite
                each other, and left Root slot size alone in a half-empty row.
                Hostname spans the row so the credentials sit together on theirs
                and the two size fields share the next one. */}
            {/* Naming an image is what lets two of the same kind coexist. The
                default used to be distro-suite-arch and nothing else, so a
                second Debian 13 amd64 build replaced the first -- taking with it
                the image a deployed machine was built from, and the LUKS
                passphrase filed under that name. Left blank, the server now
                picks a free name rather than overwriting anything. */}
            <div className="col-span-2">
              <Label>Image name (optional)</Label>
              <Input value={opts.name} onChange={(e) => set("name", e.target.value)}
                     placeholder={`${opts.distro}-${opts.suite}-${opts.arch}-ab`} />
              <div className="mt-1 flex items-center justify-between gap-3">
                <p className="text-xs text-zinc-500">
                  Leave blank and a free name is chosen — an existing image is never
                  built over by accident.
                </p>
                <label className="flex shrink-0 items-center gap-1.5 text-xs text-zinc-400">
                  <input type="checkbox" checked={opts.replace}
                         onChange={(e) => set("replace", e.target.checked)} />
                  Replace if it exists
                </label>
              </div>
            </div>
            <div className="col-span-2"><Label>Hostname</Label><Input value={opts.hostname} onChange={(e) => set("hostname", e.target.value)} /></div>
            <div><Label>Username</Label><Input value={opts.username} onChange={(e) => set("username", e.target.value)} /></div>
            <div><Label>Password</Label><Input type="password" value={opts.password} onChange={(e) => set("password", e.target.value)} placeholder="login password" /></div>
            <div><Label>Image size (GiB, 0 = smallest)</Label><Input type="number" min={0} value={opts.image_size} onChange={(e) => set("image_size", +e.target.value)} /></div>
            <div><Label>Root slot size (MiB)</Label><Input type="number" value={opts.root_size} onChange={(e) => set("root_size", +e.target.value)} /></div>
            <div className="col-span-2"><Label>Extra packages (space-separated)</Label><Input value={opts.packages} onChange={(e) => set("packages", e.target.value)} placeholder="vim curl qemu-guest-agent" /></div>
            <div className="col-span-2"><Label>SSH public key (optional)</Label><Input value={opts.ssh_key} onChange={(e) => set("ssh_key", e.target.value)} placeholder="ssh-ed25519 AAAA… user@host" /></div>
            <label className="col-span-2 flex items-center gap-2 text-sm text-zinc-300">
              <input type="checkbox" checked={opts.ssh_key_only} disabled={!opts.ssh_key} onChange={(e) => set("ssh_key_only", e.target.checked)} />
              SSH key-only (disable password login) {!opts.ssh_key && <span className="text-xs text-zinc-500">— add a key first</span>}
            </label>
          </div>

          <div className="mt-4 border-t border-zinc-800 pt-4">
            <button type="button" onClick={() => setCustomOpen((v) => !v)}
              className="flex w-full items-center justify-between text-sm font-medium text-zinc-200">
              <span>Customize the filesystem</span>
              <span className="text-xs text-zinc-500">
                {overlay && overlay.files.length > 0 ? `${overlay.files.length} file(s) staged` : "optional"}
                {customOpen ? " \u25be" : " \u25b8"}
              </span>
            </button>

            {customOpen && (
              <div className="mt-3 space-y-4">
                <div>
                  <div className="mb-1 flex items-center justify-between gap-2">
                    <Label>Files copied into the image</Label>
                    {/* Opened in a modal rather than linking to the Files page:
                        this form holds a build's worth of settings, and
                        navigating away to add a file would throw them away. */}
                    <Button size="sm" variant="secondary" onClick={() => setFilesOpen(true)}>
                      <FolderPlus size={13} /> Add or edit files
                    </Button>
                  </div>
                  {overlay && overlay.files.length > 0 ? (
                    <ul className="max-h-32 overflow-auto rounded-lg border border-zinc-800 bg-zinc-950 p-2 font-mono text-xs text-zinc-300">
                      {overlay.files.map((f) => <li key={f.path}>{f.path}</li>)}
                    </ul>
                  ) : (
                    <p className="rounded-lg border border-zinc-800 bg-zinc-950 p-3 text-xs text-zinc-500">
                      Nothing staged. Add a file and it is copied over the image root,
                      keeping its path — <code className="text-zinc-300">/etc/hosts</code> here
                      becomes <code className="text-zinc-300">/etc/hosts</code> on every machine
                      imaged from it.
                    </p>
                  )}
                  {/* The shadowing rule is the surprising part, so it is stated
                      where the files are, not only in the documentation. */}
                  <p className="mt-2 text-xs text-zinc-500">
                    A file here replaces the machine's own copy at the same path on the
                    update that delivers it. Other files in the same directory are left alone.
                  </p>
                </div>

                <div>
                  <Label>Also let the image own these paths</Label>
                  <Input value={opts.own_paths} onChange={(e) => set("own_paths", e.target.value)}
                         placeholder="/etc/hosts /etc/resolv.conf" />
                  <p className="mt-1 text-xs text-zinc-500">
                    Space-separated, for paths you are not shipping a file for but still
                    want the image to win.
                  </p>
                </div>

                {/* Writable state. The model is the important control and the
                    per-path fields are the exceptions to it, so the model comes
                    first and each option says what it costs -- picking one of
                    these wrong is not visible until an update lands on a
                    machine months later. */}
                <div className="rounded-lg border border-zinc-800 p-3">
                  <Label>Writable state</Label>
                  <select
                    value={opts.state_model}
                    onChange={(e) => set("state_model", e.target.value)}
                    className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-brand-500">
                    <option value="overlay">Overlay root — everything survives updates (default)</option>
                    <option value="stateful">Stateful — /usr read-only, /home and /var persist</option>
                    <option value="appliance">Appliance — only /data survives an update</option>
                  </select>
                  <p className="mt-1 text-xs text-zinc-500">
                    {opts.state_model === "overlay" && (
                      <>The whole root is one overlay shared by both slots. Anything done to a
                      machine, including <code className="text-zinc-300">apt install</code>, survives
                      an update.</>
                    )}
                    {opts.state_model === "stateful" && (
                      <>The slot is mounted read-only, so nothing a machine writes can shadow a
                      binary an update delivers. <code className="text-zinc-300">/home</code>,{" "}
                      <code className="text-zinc-300">/var</code> and{" "}
                      <code className="text-zinc-300">/usr/local</code> are real directories on the
                      overlay partition and survive. This is the shape ChromeOS uses.</>
                    )}
                    {opts.state_model === "appliance" && (
                      <>The slot is read-only and <code className="text-zinc-300">/var</code> reverts
                      to the image on every slot change. Only{" "}
                      <code className="text-zinc-300">/data</code> survives an update —{" "}
                      <code className="text-zinc-300">apt install</code> does not. This is the shape
                      Android and the RAUC/Mender reference layouts use.</>
                    )}
                  </p>
                  <p className="mt-2 text-xs text-amber-500/80">
                    Changing the model needs a re-image, not an update: a machine imaged with a
                    different one refuses the change at boot rather than hiding the state it
                    already has.
                  </p>

                  {/* The upper layer is shared by both slots by default, which
                      means a bad edit follows you into the other slot -- so
                      booting it recovers from a bad image but not from a bad
                      change. This is the option that fixes that, and it costs
                      the sharing, so it says both things. */}
                  <label className="mt-3 flex items-start gap-2 text-sm text-zinc-300">
                    <input type="checkbox" className="mt-0.5"
                           checked={opts.slot_private_upper}
                           onChange={(e) => set("slot_private_upper", e.target.checked)} />
                    <span>
                      Give each slot its own upper layer
                      <span className="block text-xs text-zinc-500">
                        A change that stops slot A booting cannot follow you into slot B, so the
                        other slot is a real fallback and not just an older OS. The slots then
                        share nothing the overlay covers — including{" "}
                        <code className="text-zinc-300">/home</code> and{" "}
                        <code className="text-zinc-300">/etc</code> — so list what should stay
                        shared under <em>shared across slots</em> below. Machine identity
                        (machine-id, SSH host keys) is kept shared either way.
                      </span>
                    </span>
                  </label>
                  {opts.slot_private_upper && (
                    <p className="mt-1 text-xs text-amber-500/80">
                      Like the model, this cannot be turned on or off by an update — a machine
                      imaged the other way refuses the change at boot.
                    </p>
                  )}

                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                    <div>
                      <Label>Shared across slots (persist)</Label>
                      <Input value={opts.persist_paths}
                             onChange={(e) => set("persist_paths", e.target.value)}
                             placeholder="/srv /var/lib/myapp" />
                    </div>
                    <div>
                      <Label>Private to each slot</Label>
                      <Input value={opts.slot_private_paths}
                             onChange={(e) => set("slot_private_paths", e.target.value)}
                             placeholder="/var/lib/docker" />
                    </div>
                    <div>
                      <Label>Discarded on reboot (tmpfs)</Label>
                      <Input value={opts.volatile_paths}
                             onChange={(e) => set("volatile_paths", e.target.value)}
                             placeholder="/var/tmp:256M" />
                    </div>
                    <div>
                      <Label>Reset when the slot changes</Label>
                      <Input value={opts.reset_paths}
                             onChange={(e) => set("reset_paths", e.target.value)}
                             placeholder="/var/lib/postgresql" />
                    </div>
                    <div className="sm:col-span-2">
                      <Label>Held back from that reset</Label>
                      <Input value={opts.keep_paths}
                             onChange={(e) => set("keep_paths", e.target.value)}
                             placeholder="/opt/vendor/license" />
                    </div>
                  </div>
                  <p className="mt-2 text-xs text-zinc-500">
                    Space-separated absolute paths. Use <em>private to each slot</em> for state
                    whose on-disk format is tied to the release — a shared{" "}
                    <code className="text-zinc-300">/var/lib/docker</code> hands the new version
                    the old one's data directory.
                  </p>
                </div>

                <div>
                  <Label>Run inside the image after packages are installed</Label>
                  <textarea
                    value={opts.run_script}
                    onChange={(e) => set("run_script", e.target.value)}
                    spellCheck={false}
                    rows={6}
                    placeholder={"systemctl enable my-agent\nusermod -aG dialout admin"}
                    className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-xs outline-none placeholder:text-zinc-600 focus:border-brand-500"
                  />
                  <p className="mt-1 text-xs text-zinc-500">
                    Runs as root in a chroot, so <code>systemctl enable</code> works but
                    starting a service does not \u2014 there is no running system yet. A
                    non-zero exit fails the build.
                  </p>
                </div>
              </div>
            )}
          </div>

          <div className="mt-4 border-t border-zinc-800 pt-4">
            <label className="flex items-center gap-2 text-sm font-medium text-zinc-200">
              <input type="checkbox" checked={opts.encrypt} onChange={(e) => setEncrypt(e.target.checked)} />
              Encrypt disk (LUKS2)
            </label>
            {opts.encrypt && (
              <div className="mt-3 grid grid-cols-2 gap-3">
                <div><Label>Auto-unlock method</Label><Select value={opts.unlock} onChange={(e) => setUnlock(e.target.value)}>
                  <option value="tpm2">TPM2 (recommended)</option>
                  <option value="tang">Tang / NBDE (network)</option>
                  <option value="keyfile">Keyfile (universal, weaker)</option>
                  <option value="passphrase">Passphrase (no auto-unlock)</option>
                </Select></div>
                <div><Label>LUKS passphrase (recovery)</Label><Input type="password" value={opts.luks_passphrase} onChange={(e) => set("luks_passphrase", e.target.value)} placeholder={opts.store_passphrase ? "generated" : "required"} disabled={opts.store_passphrase} /></div>
                {opts.unlock === "tang" && <div className="col-span-2"><Label>Tang server URL</Label><Input value={opts.tang_url} onChange={(e) => set("tang_url", e.target.value)} placeholder="http://tang.lan:7500" /></div>}
                {/* Offered for every unlock method, but defaulted on only where
                    the passphrase is pure recovery material. Under
                    unlock=passphrase somebody types it at every boot, and a
                    43-character random string is the wrong answer for that. */}
                {store?.configured && (
                  <label className="col-span-2 flex items-start gap-2 text-sm text-zinc-300">
                    <input type="checkbox" className="mt-0.5" checked={opts.store_passphrase}
                           onChange={(e) => set("store_passphrase", e.target.checked)} />
                    <span>
                      Generate a random passphrase and store it in {store.provider === "vault" ? "Vault" : "OpenBao"}
                      <span className="block text-xs text-zinc-500">
                        Filed under this image's name before the build starts. If the store
                        will not take it, nothing is built.
                        {opts.unlock === "passphrase" && " This one is typed at every boot — a generated passphrase makes that painful."}
                      </span>
                    </span>
                  </label>
                )}
                {store && !store.configured && (
                  <p className="col-span-2 text-xs text-zinc-500">
                    Configure a secrets manager to have this passphrase generated and kept
                    for you instead of typed here.
                  </p>
                )}
                <p className="col-span-2 text-xs text-zinc-500">
                  {opts.unlock === "tpm2" && "Sealed to each machine's TPM on first boot; no key left on disk."}
                  {opts.unlock === "tang" && "Unlocks from a Tang server on your LAN; no key on disk."}
                  {opts.unlock === "keyfile" && "Auto-unlocks anywhere, but the key sits on the same disk — weak at-rest protection."}
                  {opts.unlock === "passphrase" && "Prompts for the passphrase at every boot — most secure, not unattended."}
                </p>
              </div>
            )}
          </div>

          <Button className="mt-4 w-full" loading={running} onClick={startImage}
            title={canOperate ? undefined : "Your viewer role is read-only"}
            disabled={!canOperate || !opts.password || sizeTooSmall || (opts.encrypt && !opts.luks_passphrase && !opts.store_passphrase) || (opts.encrypt && opts.unlock === "tang" && !opts.tang_url)}>
            <Hammer size={15} /> {running ? "Building…" : "Start build"}
          </Button>
          {!opts.password && <p className="mt-2 text-xs text-amber-400">Set a login password to enable the build.</p>}
          {sizeTooSmall && <p className="mt-2 text-xs text-amber-400">
            Image too small: two {effRoot} MiB root slots + boot + overlay need ≈{Math.ceil(neededMiB / 1024)} GiB.
          </p>}
          {rootRaised && <p className="mt-2 text-xs text-zinc-500">
            Root slot will be raised to {effRoot} MiB — {opts.profile === "desktop"
              ? "a full desktop environment needs it"
              : opts.distro === "ubuntu" ? "Ubuntu needs it for the kernel and firmware"
              : "this distribution needs it"}. Expect a ≈{Math.ceil(neededMiB / 1024)} GiB image.
          </p>}
        </Card>
        <Card className="p-5">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-semibold">Build log</h2>
            <div className="flex items-center gap-2">
              {running && canOperate && <Button variant="danger" size="sm" onClick={cancel}><XCircle size={13} /> Cancel</Button>}
              {status && <Badge color={status === "success" ? "green" : status === "running" ? "amber" : "red"}>{status}</Badge>}
            </div>
          </div>
          {progress && <ProgressBar {...progress} />}
          <LogView lines={log} />
        </Card>
      </div>

      <Modal open={filesOpen} onClose={() => setFilesOpen(false)} wide
             title="Files copied into the image"
             subtitle="Applied over the image root, keeping their paths. Changes take effect on the next build.">
        {/* Refreshing the build form's own list on change keeps the summary
            behind the modal honest the moment it closes. */}
        <OverlayManager onChange={() => api.get("/overlay").then((r) => setOverlay(r.data)).catch(() => {})} />
      </Modal>
    </div>
  );
}
