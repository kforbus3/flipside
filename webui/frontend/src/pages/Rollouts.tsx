import { useEffect, useState } from "react";
import { api, apiError } from "../lib/api";
import { Card, PageHeader, Badge, Spinner, Button, Input, Modal, Label, Select } from "../components/ui";
import { Rocket, PauseCircle, PlayCircle, XCircle, AlertTriangle, Trash2 } from "lucide-react";

type Rollout = {
  id: string;
  bundle: string;
  version: string;
  bundle_url: string;
  description: string;
  target: { groups: string[]; hosts: string[]; all: boolean };
  strategy: { canary: number; batch_size: number; soak_seconds: number; max_failures: number };
  window: { start?: string; end?: string; days?: number[] } | null;
  state: "running" | "paused" | "halted" | "completed" | "cancelled";
  halt_reason: string;
  created: number;
  created_by: string;
  total: number;
  done: number;
  counts: Record<string, number>;
  machines: Record<string, { state: string; error?: string; at?: number }>;
};

const STATE_COLOR: Record<string, string> = {
  running: "green", paused: "amber", halted: "red",
  completed: "blue", cancelled: "zinc",
};

// The order machines move through. Shown as a bar so a stalled rollout is
// visible as a bar that is not moving, rather than as a number nobody watches.
const PHASES: { key: string; label: string; cls: string }[] = [
  { key: "verified",   label: "Verified",   cls: "bg-emerald-500" },
  { key: "rebooting",  label: "Rebooting",  cls: "bg-sky-500" },
  { key: "installing", label: "Installing", cls: "bg-sky-600" },
  { key: "offered",    label: "Offered",    cls: "bg-brand-500" },
  { key: "failed",     label: "Failed",     cls: "bg-red-500" },
  { key: "pending",    label: "Waiting",    cls: "bg-zinc-700" },
];

export default function Rollouts() {
  const [rows, setRows] = useState<Rollout[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [detail, setDetail] = useState<Rollout | null>(null);
  const [error, setError] = useState("");

  async function load() {
    try {
      const { data } = await api.get("/rollouts");
      setRows(data.rollouts);
      setError("");
    } catch (e) { setError(apiError(e)); }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  async function steer(id: string, verb: string) {
    try { await api.post(`/rollouts/${id}/${verb}`); load(); }
    catch (e) { setError(apiError(e)); }
  }

  async function remove(id: string) {
    try { await api.delete(`/rollouts/${id}`); load(); }
    catch (e) { setError(apiError(e)); }
  }

  return (
    <div>
      <PageHeader
        title="Rollouts"
        subtitle="Put a bundle on a group of machines, a few at a time"
        actions={<Button onClick={() => setCreating(true)}><Rocket size={14} /> New rollout</Button>}
      />

      {error && <Card className="mb-6 border-red-500/40 bg-red-500/10">
        <p className="p-4 text-sm text-red-300">{error}</p></Card>}
      {rows === null && <Spinner />}

      {rows !== null && rows.length === 0 && (
        <Card>
          <div className="py-14 text-center">
            <Rocket className="mx-auto text-zinc-700" size={30} />
            <p className="mt-3 text-sm text-zinc-400">No rollouts yet.</p>
            <p className="mx-auto mt-1 max-w-lg text-xs text-zinc-500">
              A rollout records which bundle a group of machines should be running.
              Nothing is sent anywhere — each machine picks the update up on its next
              check-in, which is what makes this work for machines that have left the
              provisioning network.
            </p>
          </div>
        </Card>
      )}

      <div className="space-y-4">
        {(rows || []).map((r) => (
          <Card key={r.id}>
            <div className="flex flex-wrap items-center gap-3 border-b border-zinc-800 px-5 py-3">
              <div className="mr-auto min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold text-zinc-100">{r.version}</span>
                  <Badge color={STATE_COLOR[r.state]}>{r.state}</Badge>
                  <span className="font-mono text-[11px] text-zinc-600">{r.id}</span>
                </div>
                <div className="mt-0.5 truncate text-xs text-zinc-500">
                  {r.bundle} → {r.target.all ? "the whole fleet"
                    : [...r.target.groups, ...r.target.hosts].join(", ") || "nothing"}
                  {" · "}{r.done} of {r.total} done
                </div>
              </div>
              {r.state === "running" && (
                <Button size="sm" variant="secondary" onClick={() => steer(r.id, "pause")}>
                  <PauseCircle size={13} /> Pause
                </Button>
              )}
              {(r.state === "paused" || r.state === "halted") && (
                <Button size="sm" variant="secondary" onClick={() => steer(r.id, "resume")}>
                  <PlayCircle size={13} /> Resume
                </Button>
              )}
              {(r.state === "running" || r.state === "paused" || r.state === "halted") && (
                <Button size="sm" variant="secondary" onClick={() => steer(r.id, "cancel")}>
                  <XCircle size={13} /> Cancel
                </Button>
              )}
              {r.state !== "running" && (
                <Button size="sm" variant="ghost" onClick={() => remove(r.id)}>
                  <Trash2 size={13} />
                </Button>
              )}
              <Button size="sm" variant="ghost" onClick={() => setDetail(r)}>Machines</Button>
            </div>

            {r.state === "halted" && (
              <p className="flex items-start gap-2 border-b border-zinc-800 bg-red-500/10 px-5 py-2.5 text-xs text-red-300">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                <span>
                  {r.halt_reason} Resuming continues with the machines that have not
                  been tried; the ones that failed stay failed and the failure budget
                  starts counting again from here.
                </span>
              </p>
            )}

            <div className="px-5 py-3">
              <div className="flex h-2 overflow-hidden rounded-full bg-zinc-800">
                {PHASES.map((p) => {
                  const n = r.counts[p.key] || 0;
                  if (!n || !r.total) return null;
                  return <div key={p.key} className={p.cls}
                              style={{ width: `${(n / r.total) * 100}%` }} title={`${p.label}: ${n}`} />;
                })}
              </div>
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-zinc-500">
                {PHASES.map((p) => (r.counts[p.key] ? (
                  <span key={p.key} className="inline-flex items-center gap-1.5">
                    <span className={`inline-block h-2 w-2 rounded-full ${p.cls}`} />
                    {p.label} {r.counts[p.key]}
                  </span>
                ) : null))}
                <span className="ml-auto">
                  canary {r.strategy.canary} · batches of {r.strategy.batch_size} ·
                  soak {Math.round(r.strategy.soak_seconds / 60)}m ·
                  stop after {r.strategy.max_failures} failures
                  {r.window?.start && ` · ${r.window.start}–${r.window.end}`}
                </span>
              </div>
            </div>
          </Card>
        ))}
      </div>

      <NewRollout open={creating} onClose={() => setCreating(false)}
                  onCreated={() => { setCreating(false); load(); }} />
      <Detail rollout={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

function Detail({ rollout, onClose }: { rollout: Rollout | null; onClose: () => void }) {
  if (!rollout) return null;
  const entries = Object.entries(rollout.machines);
  return (
    <Modal open={!!rollout} onClose={onClose} wide
           title={`${rollout.version} · ${rollout.id}`}
           subtitle={`${entries.length} machines`}>
      <div className="max-h-[26rem] overflow-y-auto">
        <table className="w-full text-left text-sm">
          <thead className="sticky top-0 bg-zinc-900">
            <tr className="border-b border-zinc-800 text-xs uppercase text-zinc-500">
              <th className="py-2 pr-3">Machine</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Detail</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/70">
            {entries.map(([id, m]) => (
              <tr key={id}>
                <td className="py-2 pr-3 font-mono text-xs text-zinc-300">{id}</td>
                <td className="px-3 py-2">
                  <Badge color={m.state === "verified" ? "green" : m.state === "failed" ? "red"
                              : m.state === "pending" ? "zinc" : "blue"}>{m.state}</Badge>
                </td>
                <td className="px-3 py-2 text-xs text-zinc-500">{m.error || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Modal>
  );
}

function NewRollout({ open, onClose, onCreated }:
  { open: boolean; onClose: () => void; onCreated: () => void }) {
  const [bundles, setBundles] = useState<{ name: string; version?: string }[]>([]);
  const [groups, setGroups] = useState<{ name: string; hosts: number }[]>([]);
  const [bundle, setBundle] = useState("");
  const [group, setGroup] = useState("");
  const [canary, setCanary] = useState(1);
  const [batch, setBatch] = useState(10);
  const [soak, setSoak] = useState(15);
  const [maxFail, setMaxFail] = useState(2);
  const [useWindow, setUseWindow] = useState(false);
  const [start, setStart] = useState("22:00");
  const [end, setEnd] = useState("04:00");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setError("");
    api.get("/bundles").then(({ data }) => setBundles(data.bundles || []));
    api.get("/fleet/groups").then(({ data }) => setGroups(data.groups || []));
  }, [open]);

  async function create() {
    setSaving(true);
    try {
      await api.post("/rollouts", {
        bundle, groups: group ? [group] : [], all: !group,
        strategy: { canary, batch_size: batch, soak_seconds: soak * 60, max_failures: maxFail },
        window: useWindow ? { start, end, days: [0, 1, 2, 3, 4, 5, 6] } : null,
      });
      onCreated();
    } catch (e) { setError(apiError(e)); } finally { setSaving(false); }
  }

  return (
    <Modal open={open} onClose={onClose} title="New rollout"
           subtitle="Machines pick this up on their next check-in">
      {error && <p className="mb-3 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300">{error}</p>}
      <div className="space-y-4">
        <div>
          <Label>Bundle</Label>
          <Select value={bundle} onChange={(e) => setBundle(e.target.value)}>
            <option value="">Choose a bundle…</option>
            {bundles.map((b) => (
              <option key={b.name} value={b.name}>
                {b.name}{b.version ? ` — ${b.version}` : " — no version recorded"}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Target</Label>
          <Select value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">The whole fleet</option>
            {groups.map((g) => <option key={g.name} value={g.name}>{g.name} ({g.hosts} machines)</option>)}
          </Select>
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div>
            <Label>Canary</Label>
            <Input type="number" min={0} value={canary}
                   onChange={(e) => setCanary(+e.target.value)} />
          </div>
          <div>
            <Label>Then batches of</Label>
            <Input type="number" min={1} value={batch} onChange={(e) => setBatch(+e.target.value)} />
          </div>
          <div>
            <Label>Soak (min)</Label>
            <Input type="number" min={0} value={soak} onChange={(e) => setSoak(+e.target.value)} />
          </div>
          <div>
            <Label>Stop after</Label>
            <Input type="number" min={0} value={maxFail} onChange={(e) => setMaxFail(+e.target.value)} />
          </div>
        </div>
        <p className="text-xs text-zinc-500">
          A machine counts as done only when it comes back on the new version and passes
          its health check — not when the install returns. An update that installs
          cleanly and then fails to boot is exactly what the canary and the soak are
          there to catch, before the rest of the fleet gets it.
        </p>

        <label className="flex items-center gap-2 text-sm text-zinc-300">
          <input type="checkbox" checked={useWindow} onChange={(e) => setUseWindow(e.target.checked)} />
          Only start machines during a maintenance window
        </label>
        {useWindow && (
          <div className="grid grid-cols-2 gap-3">
            <div><Label>From</Label><Input type="time" value={start} onChange={(e) => setStart(e.target.value)} /></div>
            <div><Label>To</Label><Input type="time" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
            <p className="col-span-2 text-xs text-zinc-500">
              Server local time, and it gates when an install <em>starts</em>. A long
              download can carry the reboot past the end of the window.
            </p>
          </div>
        )}
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button loading={saving} disabled={!bundle} onClick={create}>Create rollout</Button>
      </div>
    </Modal>
  );
}
