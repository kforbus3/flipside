import { useEffect, useRef, useState } from "react";
import { api, apiError, fmtBytes } from "../lib/api";
import { Card, PageHeader, Button, Spinner, Badge } from "../components/ui";
import { useToast } from "../components/Toast";
import { Download, Upload, AlertTriangle, ShieldAlert, RotateCcw } from "lucide-react";

type Manifest = {
  version: string;
  created: string;
  bytes: number;
  files: { path: string; size: number; sha256: string; mode: string }[];
};

export default function Backup() {
  const toast = useToast();
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<{ name: string; data: File; info: any } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  async function load() {
    try {
      const { data } = await api.get("/backup/manifest");
      setManifest(data);
      setError("");
    } catch (e) { setError(apiError(e)); }
  }
  useEffect(() => { load(); }, []);

  async function download() {
    setBusy(true);
    try {
      const r = await api.get("/backup", { responseType: "blob" });
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a");
      a.href = url;
      a.download = (r.headers["content-disposition"] || "").match(/filename="([^"]+)"/)?.[1]
        || "flipside-backup.tar.gz";
      a.click();
      URL.revokeObjectURL(url);
      toast.success("Backup downloaded — it contains the signing key; store it accordingly");
    } catch (e) { toast.error(apiError(e)); } finally { setBusy(false); }
  }

  // Inspect before restoring, always. What is in the file and when it was taken
  // is the question somebody actually needs answered before replacing the
  // server's state with it, and answering it must not be able to change
  // anything.
  async function choose(file: File) {
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const { data } = await api.post("/backup/inspect", form);
      setPending({ name: file.name, data: file, info: data });
    } catch (e) { toast.error(apiError(e)); } finally { setBusy(false); }
  }

  async function restore() {
    if (!pending) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", pending.data);
      const { data } = await api.post("/backup/restore", form);
      toast.success(`Restored ${data.restored.length} files. ${data.note}`);
      setPending(null);
      if (fileRef.current) fileRef.current.value = "";
      load();
    } catch (e) { toast.error(apiError(e)); } finally { setBusy(false); }
  }

  return (
    <div>
      <PageHeader title="Backup and restore"
                  subtitle="Everything this server cannot rebuild, as one file" />

      {error && <Card className="mb-6 border-red-500/40 bg-red-500/10">
        <p className="p-4 text-sm text-red-300">{error}</p></Card>}

      <Card className="mb-6 border-amber-500/40 bg-amber-500/10">
        <div className="flex gap-3 p-4 text-sm text-amber-200">
          <ShieldAlert size={18} className="mt-0.5 shrink-0" />
          <div>
            <p className="font-medium">This file is as sensitive as the update signing key.</p>
            <p className="mt-1 text-amber-100/80">
              It contains <span className="font-mono">rauc-keys/key.pem</span>, every
              password hash, every live API token, and the secrets-manager token. Losing
              the signing key means no machine already deployed can ever be updated
              again; leaking it means anyone can sign an update those machines will
              install. It is not encrypted — put it somewhere that encrypts at rest.
            </p>
          </div>
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <div className="border-b border-zinc-800 px-5 py-3">
            <h2 className="text-sm font-semibold text-zinc-200">Take a backup</h2>
          </div>
          <div className="p-5">
            {manifest === null ? <Spinner /> : (
              <>
                <div className="mb-3 flex items-center gap-2 text-sm text-zinc-400">
                  <Badge color="brand">{manifest.files.length} files</Badge>
                  <span>{fmtBytes(manifest.bytes)}</span>
                </div>
                {/* Listed rather than summarised: a backup's one failure mode is
                    that it quietly stops containing something, and that is only
                    ever discovered during a restore. */}
                <div className="mb-4 max-h-56 overflow-y-auto rounded-lg border border-zinc-800">
                  <table className="w-full text-left text-xs">
                    <tbody className="divide-y divide-zinc-800/60">
                      {manifest.files.map((f) => (
                        <tr key={f.path}>
                          <td className="px-3 py-1.5 font-mono text-zinc-300">{f.path}</td>
                          <td className="px-3 py-1.5 text-right tabular-nums text-zinc-500">
                            {fmtBytes(f.size)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Button loading={busy} onClick={download}>
                  <Download size={14} /> Download backup
                </Button>
                <p className="mt-3 text-xs text-zinc-500">
                  Images and bundles are deliberately left out — they are large and they
                  are rebuilt from the repository. Everything here is not.
                </p>
              </>
            )}
          </div>
        </Card>

        <Card>
          <div className="border-b border-zinc-800 px-5 py-3">
            <h2 className="text-sm font-semibold text-zinc-200">Restore</h2>
          </div>
          <div className="p-5">
            <input ref={fileRef} type="file" accept=".gz,.tgz,application/gzip"
                   className="block w-full text-sm text-zinc-400 file:mr-3 file:rounded-lg
                              file:border-0 file:bg-zinc-800 file:px-3 file:py-2
                              file:text-sm file:text-zinc-200 hover:file:bg-zinc-700"
                   onChange={(e) => { const f = e.target.files?.[0]; if (f) choose(f); }} />

            {pending && (
              <div className="mt-4">
                <div className="rounded-lg border border-zinc-800 p-3 text-sm">
                  <p className="text-zinc-200">{pending.name}</p>
                  <p className="mt-0.5 text-xs text-zinc-500">
                    Taken {pending.info.created} by version {pending.info.version} ·{" "}
                    {pending.info.files.length} files
                  </p>
                </div>
                <div className="mt-3 flex items-start gap-2 rounded-lg border border-amber-500/40
                                bg-amber-500/10 p-3 text-xs text-amber-200">
                  <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                  <span>
                    This replaces users, sessions, groups, assignments and the audit log
                    with what is in the archive. If it predates your account or your
                    current password, you will need the credentials that were current
                    when it was taken. The present state is saved aside first.
                  </span>
                </div>
                <div className="mt-3 flex gap-2">
                  <Button variant="danger" loading={busy} onClick={restore}>
                    <RotateCcw size={14} /> Restore this backup
                  </Button>
                  <Button variant="secondary" onClick={() => {
                    setPending(null);
                    if (fileRef.current) fileRef.current.value = "";
                  }}>Cancel</Button>
                </div>
              </div>
            )}

            {!pending && (
              <p className="mt-4 flex items-start gap-2 text-xs text-zinc-500">
                <Upload size={14} className="mt-0.5 shrink-0" />
                <span>
                  The archive is checked in full — every file against its recorded
                  checksum — before anything is written. A damaged one changes nothing.
                </span>
              </p>
            )}

            <p className="mt-5 border-t border-zinc-800 pt-4 text-xs text-zinc-500">
              If the UI itself will not start, do the same from a shell:
              <span className="mt-1 block font-mono text-zinc-400">
                ./scripts/flipside-backup.sh restore FILE
              </span>
            </p>
          </div>
        </Card>
      </div>
    </div>
  );
}
