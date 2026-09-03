import { useEffect, useState } from "react";
import { HardDrive, Download, Trash2, RefreshCw, Rocket, Lock, KeyRound, FileSearch, Search } from "lucide-react";
import { api, apiError, fmtBytes } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useToast } from "../components/Toast";
import { Button, Card, PageHeader, Spinner, Badge, Input, Modal } from "../components/ui";
import { secretName } from "./Secrets";

interface Meta { distro?: string; suite?: string; encrypted?: boolean; unlock?: string; created?: string;
                 version?: string; packages?: number; sbom?: string; }
interface Img { name: string; size: number; created: string; sha256?: string; meta?: Meta; }

export default function Images() {
  const toast = useToast();
  // Deploy writes the provisioning server config, which is an admin call;
  // delete is an operator one. Download and the listing are for everyone.
  const { canOperate, isAdmin } = useAuth();
  const [images, setImages] = useState<Img[]>([]);
  const [imagerReady, setImagerReady] = useState(false);
  const [deployed, setDeployed] = useState("");
  const [loading, setLoading] = useState(true);
  const [inStore, setInStore] = useState<Set<string>>(new Set());
  const [revealed, setRevealed] = useState<Record<string, string>>({});
  const [searching, setSearching] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const [{ data }, cfg] = await Promise.all([api.get("/images"), api.get("/server/config")]);
      setImages(data.images); setImagerReady(data.imager_ready); setDeployed(cfg.data.IMAGE_FILE || "");
    } catch (e) { toast.error(apiError(e)); } finally { setLoading(false); }
    try {
      const { data } = await api.get("/secrets/entries");
      setInStore(new Set<string>(data.entries || []));
    } catch { /* no store configured — encrypted images just show the lock */ }
  }
  useEffect(() => { load(); }, []);

  async function remove(name: string) {
    if (!confirm(`Delete ${name}?`)) return;
    try { await api.delete(`/images/${encodeURIComponent(name)}`); toast.success("Deleted"); load(); }
    catch (e) { toast.error(apiError(e)); }
  }
  async function download(name: string) {
    try {
      const res = await api.get(`/images/${encodeURIComponent(name)}/download`, { responseType: "blob" });
      const url = URL.createObjectURL(res.data);
      const a = document.createElement("a"); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url);
    } catch (e) { toast.error(apiError(e)); }
  }
  // Shown here as well as on the Secrets page because this is the list an
  // operator is looking at when a machine built from one of these rows will not
  // unlock, and the recovery key should be one click from the image it opens.
  async function reveal(name: string) {
    try {
      const { data } = await api.get(`/secrets/passphrase/${encodeURIComponent(name)}`);
      setRevealed((r) => ({ ...r, [name]: data.passphrase }));
    } catch (e) { toast.error(apiError(e)); }
  }
  async function deploy(name: string) {
    try {
      const { data: cfg } = await api.get("/server/config");
      await api.put("/server/config", { ...cfg, IMAGE_FILE: name });
      setDeployed(name);
      toast.success(`${name} set as the deploy image — (re)start the server on the Provisioning page`);
    } catch (e) { toast.error(apiError(e)); }
  }

  return (
    <div>
      <PageHeader title="Images" subtitle="Built A/B disk images" actions={
        <>
          <Button variant="secondary" size="sm" onClick={() => setSearching(true)}><FileSearch size={13} /> Find a package</Button>
          <Badge color={imagerReady ? "green" : "amber"}>imager {imagerReady ? "ready" : "missing"}</Badge>
          <Button variant="secondary" size="sm" onClick={load}><RefreshCw size={13} /> Refresh</Button>
        </>
      } />
      <Card>
        {loading ? <Spinner /> : images.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-16 text-center text-zinc-500">
            <HardDrive size={32} /><p className="text-sm">No images yet — build one from the Build page.</p>
          </div>
        ) : (
          <table className="w-full text-left text-sm">
            <thead><tr className="border-b border-zinc-800 text-xs uppercase tracking-wide text-zinc-500">
              <th className="px-4 py-2.5 font-medium">Name</th><th className="px-4 py-2.5 font-medium">System</th>
              <th className="px-4 py-2.5 font-medium">Size</th><th className="px-4 py-2.5 font-medium">Contents</th>
              <th className="px-4 py-2.5 font-medium">SHA256</th>
              <th className="px-4 py-2.5 font-medium">Created</th><th className="px-4 py-2.5"></th>
            </tr></thead>
            <tbody className="divide-y divide-zinc-800/70">
              {images.map((m) => (
                <tr key={m.name} className="hover:bg-zinc-800/40">
                  <td className="px-4 py-3 font-medium text-zinc-200">
                    <div className="flex items-center gap-2">{m.name}
                      {m.meta?.encrypted && <span title={`LUKS2, unlock: ${m.meta.unlock}`}><Lock size={13} className="text-amber-400" /></span>}
                      {m.meta?.encrypted && inStore.has(secretName(m.name)) && (
                        <button type="button" onClick={() => reveal(m.name)}
                                title="Recovery passphrase is in the secrets manager — click to reveal"
                                className="text-emerald-400 hover:text-emerald-300">
                          <KeyRound size={13} />
                        </button>
                      )}
                      {m.name === deployed && <Badge color="green">deploying</Badge>}
                    </div>
                    {revealed[m.name] && (
                      <code className="mt-1 block break-all font-mono text-xs font-normal text-amber-300">
                        {revealed[m.name]}
                      </code>
                    )}
                  </td>
                  <td className="px-4 py-3 text-zinc-400">{m.meta ? `${m.meta.distro} ${m.meta.suite}` : "—"}</td>
                  <td className="px-4 py-3 text-zinc-400">{fmtBytes(m.size)}</td>
                  <td className="px-4 py-3 text-xs text-zinc-400">
                    {m.meta?.packages ? (
                      <>
                        <span className="tabular-nums">{m.meta.packages} packages</span>
                        <span className="mt-0.5 block text-zinc-600">
                          SBOM{" "}
                          <a className="text-brand-400 hover:text-brand-300"
                             href={`/api/sbom/${encodeURIComponent(m.name)}?format=spdx`}>SPDX</a>{" · "}
                          <a className="text-brand-400 hover:text-brand-300"
                             href={`/api/sbom/${encodeURIComponent(m.name)}?format=cyclonedx`}>CycloneDX</a>
                        </span>
                      </>
                    ) : (
                      /* Not "0 packages": an image built before SBOMs existed has
                         the same empty sidecar field as one whose capture failed,
                         and neither is a claim that the image contains nothing. */
                      <span className="text-zinc-600" title="Built before SBOMs existed, or the capture failed. Rebuild to get one.">no SBOM</span>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-zinc-500" title={m.sha256}>{m.sha256 ? m.sha256.slice(0, 12) + "…" : "—"}</td>
                  <td className="px-4 py-3 text-zinc-400">{new Date(m.meta?.created || m.created).toLocaleString()}</td>
                  <td className="px-4 py-3"><div className="flex justify-end gap-1">
                    {m.name !== deployed && <Button size="sm" disabled={!isAdmin} title={isAdmin ? undefined : "Deploying changes the server configuration — admin only"} onClick={() => deploy(m.name)}><Rocket size={13} /> Deploy</Button>}
                    <Button size="sm" variant="secondary" onClick={() => download(m.name)}><Download size={13} /></Button>
                    <Button size="sm" variant="ghost" disabled={!canOperate} title={canOperate ? undefined : "Your viewer role is read-only"} onClick={() => remove(m.name)}><Trash2 size={14} className="text-red-400" /></Button>
                  </div></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      <PackageSearch open={searching} onClose={() => setSearching(false)} />
    </div>
  );
}

/** "Which of our images have the vulnerable openssl" — one request, every
 *  artifact, images and bundles alike. The alternative is mounting each image
 *  in turn, and this question is only ever asked when there is no time for that. */
function PackageSearch({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [pkg, setPkg] = useState("");
  const [ver, setVer] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState("");

  async function run(e?: React.FormEvent) {
    e?.preventDefault();
    if (!pkg.trim()) return;
    setBusy(true); setErr("");
    try {
      const { data } = await api.get("/sbom", { params: { package: pkg.trim(), version: ver.trim() } });
      setRes(data);
    } catch (e2) { setErr(apiError(e2)); } finally { setBusy(false); }
  }

  return (
    <Modal open={open} onClose={onClose} wide title="Find a package"
           subtitle="Searches every image and bundle that has an SBOM">
      <form onSubmit={run} className="flex flex-wrap gap-2">
        <Input className="min-w-[16rem] flex-1" autoFocus value={pkg}
               onChange={(e) => setPkg(e.target.value)}
               placeholder="Package name — a regular expression, e.g. ^openssl$ or ^libssl" />
        <Input className="w-44" value={ver} onChange={(e) => setVer(e.target.value)}
               placeholder="Version contains…" />
        <Button type="submit" loading={busy}><Search size={13} /> Search</Button>
      </form>

      {err && <p className="mt-3 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300">{err}</p>}

      {res && (
        <div className="mt-4">
          <p className="mb-2 text-xs text-zinc-500">
            {/* Distinguishing "nothing matched" from "nothing had an SBOM to match
                against" matters: they look identical and mean opposite things. */}
            {res.searched === 0
              ? "No artifact on this server has an SBOM yet — rebuild an image to get one."
              : `${res.results.length} of ${res.searched} artifacts match.`}
          </p>
          <div className="max-h-[24rem] space-y-3 overflow-y-auto">
            {res.results.map((r: any) => (
              <div key={r.kind + r.artifact} className="rounded-lg border border-zinc-800 p-3">
                <div className="flex items-center gap-2">
                  <Badge color={r.kind === "bundle" ? "blue" : "brand"}>{r.kind}</Badge>
                  <span className="text-sm text-zinc-200">{r.artifact}</span>
                  <span className="text-xs text-zinc-500">{r.match_count} matching</span>
                </div>
                <table className="mt-2 w-full text-left text-xs">
                  <tbody className="divide-y divide-zinc-800/60">
                    {r.matches.map((m: any) => (
                      <tr key={m.package + m.arch}>
                        <td className="py-1 pr-4 text-zinc-300">{m.package}</td>
                        <td className="py-1 pr-4 font-mono text-zinc-400">{m.version}</td>
                        <td className="py-1 text-zinc-600">{m.arch}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        </div>
      )}
    </Modal>
  );
}
