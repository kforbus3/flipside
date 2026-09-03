import { useEffect, useMemo, useState } from "react";
import { api, apiError } from "../lib/api";
import { Card, PageHeader, Badge, Spinner, Button, Input, Modal, Label, Select } from "../components/ui";
import { Server, CheckCircle2, AlertTriangle, HelpCircle, WifiOff, Tags } from "lucide-react";

type Machine = {
  id: string;
  hostname: string;
  address: string;
  image: string;
  slot: string;
  version: string;
  groups: string[];
  label: string;
  paused: boolean;
  presence: "online" | "stale" | "offline" | "unknown";
  provision_state: string;
  last_seen: number | null;
  imaged_at: number | null;
  booted_at: number | null;
  update_state?: string;
  update_error?: string;
  health?: string;
};

// Presence answers "is what this row says still true", which is a different
// question from whether the machine is healthy — a machine can be perfectly
// fine and simply live somewhere this server cannot hear from it.
const PRESENCE = {
  online:  { badge: "green", label: "Online",   icon: <CheckCircle2 size={16} className="text-emerald-400" /> },
  stale:   { badge: "amber", label: "Stale",    icon: <AlertTriangle size={16} className="text-amber-400" /> },
  offline: { badge: "red",   label: "Offline",  icon: <WifiOff size={16} className="text-red-400" /> },
  unknown: { badge: "zinc",  label: "No agent", icon: <HelpCircle size={16} className="text-zinc-500" /> },
} as const;

function when(ts: number | null | undefined) {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

export default function Fleet() {
  const [rows, setRows] = useState<Machine[] | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [versions, setVersions] = useState<Record<string, number>>({});
  const [controlUrl, setControlUrl] = useState("");
  const [groups, setGroups] = useState<{ name: string; hosts: number }[]>([]);
  const [filter, setFilter] = useState("");
  const [groupFilter, setGroupFilter] = useState("");
  const [editing, setEditing] = useState<Machine | null>(null);
  const [error, setError] = useState("");

  async function load() {
    try {
      const [fleet, grp] = await Promise.all([
        api.get("/fleet"),
        api.get("/fleet/groups"),
      ]);
      setRows(fleet.data.machines);
      setCounts(fleet.data.counts || {});
      setVersions(fleet.data.versions || {});
      setControlUrl(fleet.data.control_url || "");
      setGroups(grp.data.groups || []);
      setError("");
    } catch (e) { setError(apiError(e)); }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);

  const shown = useMemo(() => (rows || []).filter((m) => {
    if (groupFilter && !m.groups.includes(groupFilter)) return false;
    if (!filter) return true;
    const hay = `${m.id} ${m.hostname} ${m.label} ${m.version} ${m.address} ${m.groups.join(" ")}`;
    return hay.toLowerCase().includes(filter.toLowerCase());
  }), [rows, filter, groupFilter]);

  const noAgents = (counts.unknown || 0) > 0 && !(counts.online || 0);

  return (
    <div>
      <PageHeader
        title="Fleet"
        subtitle="Every machine, what it is running, and when it last said so"
      />

      {error && <Card className="mb-6 border-red-500/40 bg-red-500/10">
        <p className="p-4 text-sm text-red-300">{error}</p></Card>}
      {rows === null && <Spinner />}

      {rows !== null && (
        <>
          {/* The one configuration mistake that makes the whole control plane
              silently useless: machines are imaged on the provisioning segment
              and told to report there, then moved somewhere that cannot reach
              it. They keep running perfectly and are simply never heard from,
              which looks identical to nothing having been set up at all. */}
          {!controlUrl && (
            <Card className="mb-6 border-amber-500/40 bg-amber-500/10">
              <div className="p-4 text-sm text-amber-200">
                <p className="flex items-center gap-2 font-medium">
                  <AlertTriangle size={15} /> No control-plane address is set.
                </p>
                <p className="mt-1.5 text-amber-100/80">
                  Machines fall back to reporting at the address they were imaged
                  from — which is on the provisioning network they leave. Set
                  <span className="font-mono"> CONTROL_URL</span> on the Provisioning
                  page to an address the fleet can reach from where it actually lives,
                  and machines will move themselves to it on their next check-in.
                </p>
              </div>
            </Card>
          )}

          {noAgents && (
            <Card className="mb-6 border-zinc-700 bg-zinc-800/40">
              <p className="p-4 text-sm text-zinc-300">
                No machine has run an agent yet. Machines imaged before the control
                plane existed do not have one; re-image them, or run
                <span className="font-mono"> ab-agent --set-server {controlUrl || "<url>"} </span>
                on each and enable <span className="font-mono">ab-agent.timer</span>.
              </p>
            </Card>
          )}

          <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {(["online", "stale", "offline", "unknown"] as const).map((k) => (
              <Card key={k} className="px-4 py-3">
                <div className="flex items-center gap-2">
                  {PRESENCE[k].icon}
                  <div>
                    <div className="text-lg font-semibold tabular-nums text-zinc-100">{counts[k] || 0}</div>
                    <div className="text-xs text-zinc-500">{PRESENCE[k].label}</div>
                  </div>
                </div>
              </Card>
            ))}
          </div>

          {Object.keys(versions).length > 1 && (
            <Card className="mb-4">
              <div className="flex flex-wrap items-center gap-2 px-5 py-3">
                <span className="text-xs uppercase text-zinc-500">Versions in the field</span>
                {Object.entries(versions).sort((a, b) => b[1] - a[1]).map(([v, n]) => (
                  <Badge key={v} color="brand">{v} · {n}</Badge>
                ))}
              </div>
            </Card>
          )}

          <Card>
            <div className="flex flex-wrap items-center gap-2 border-b border-zinc-800 px-5 py-3">
              <h2 className="mr-auto text-sm font-semibold text-zinc-200">
                Machines {rows.length > 0 && <Badge color="brand">{shown.length}</Badge>}
              </h2>
              <Select className="w-auto" value={groupFilter} onChange={(e) => setGroupFilter(e.target.value)}>
                <option value="">All groups</option>
                {groups.map((g) => <option key={g.name} value={g.name}>{g.name} ({g.hosts})</option>)}
              </Select>
              <Input className="w-auto min-w-[14rem]" placeholder="Filter…"
                     value={filter} onChange={(e) => setFilter(e.target.value)} />
            </div>

            {shown.length === 0 ? (
              <div className="py-14 text-center">
                <Server className="mx-auto text-zinc-700" size={30} />
                <p className="mt-3 text-sm text-zinc-400">
                  {rows.length ? "No machines match that filter." : "No machines yet."}
                </p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr className="border-b border-zinc-800 text-xs uppercase text-zinc-500">
                      <th className="px-5 py-2">Machine</th>
                      <th className="px-3 py-2">Presence</th>
                      <th className="px-3 py-2">Groups</th>
                      <th className="px-3 py-2">Version</th>
                      <th className="px-3 py-2">Slot</th>
                      <th className="px-3 py-2">Last seen</th>
                      <th className="px-3 py-2"></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800/70">
                    {shown.map((m) => {
                      const st = PRESENCE[m.presence] || PRESENCE.unknown;
                      return (
                        <tr key={m.id}>
                          <td className="px-5 py-2.5">
                            <div className="flex items-center gap-2">
                              {st.icon}
                              <div className="min-w-0">
                                <div className="text-xs text-zinc-200">
                                  {m.label || m.hostname || m.id}
                                  {m.paused && <span className="ml-2"><Badge color="amber">held</Badge></span>}
                                </div>
                                <div className="font-mono text-[11px] text-zinc-500">{m.id}</div>
                              </div>
                            </div>
                          </td>
                          <td className="px-3 py-2.5">
                            <Badge color={st.badge}>{st.label}</Badge>
                            {m.health === "degraded" &&
                              <span className="ml-1"><Badge color="red">degraded</Badge></span>}
                          </td>
                          <td className="px-3 py-2.5">
                            <div className="flex flex-wrap gap-1">
                              {m.groups.length
                                ? m.groups.map((g) => <Badge key={g}>{g}</Badge>)
                                : <span className="text-xs text-zinc-600">—</span>}
                            </div>
                          </td>
                          <td className="px-3 py-2.5 text-xs text-zinc-400">
                            {m.version || "—"}
                            {m.update_state && m.update_state !== "idle" && (
                              <div className="text-[11px] text-sky-400">{m.update_state}</div>
                            )}
                            {m.update_error && (
                              <div className="max-w-[18rem] truncate text-[11px] text-red-400"
                                   title={m.update_error}>{m.update_error}</div>
                            )}
                          </td>
                          <td className="px-3 py-2.5 text-zinc-400">{m.slot || "—"}</td>
                          <td className="px-3 py-2.5 text-xs tabular-nums text-zinc-400">
                            {when(m.last_seen)}
                          </td>
                          <td className="px-3 py-2.5 text-right">
                            <Button size="sm" variant="ghost" onClick={() => setEditing(m)}>
                              <Tags size={13} /> Edit
                            </Button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <p className="mt-4 text-xs text-zinc-600">
            Machines check in on their own timer; nothing is sent to them. A machine
            that stops checking in goes stale and then offline — it has not necessarily
            failed, but what this page says about it has stopped being current.
          </p>

          <EditHost machine={editing} groups={groups.map((g) => g.name)}
                    onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />
        </>
      )}
    </div>
  );
}

function EditHost({ machine, groups, onClose, onSaved }: {
  machine: Machine | null; groups: string[]; onClose: () => void; onSaved: () => void;
}) {
  const [label, setLabel] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [fresh, setFresh] = useState("");
  const [paused, setPaused] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!machine) return;
    setLabel(machine.label || "");
    setChosen(machine.groups || []);
    setPaused(!!machine.paused);
    setFresh("");
    setError("");
  }, [machine]);

  if (!machine) return null;
  const all = Array.from(new Set([...groups, ...chosen])).sort();

  async function save() {
    setSaving(true);
    try {
      const merged = fresh.trim() ? Array.from(new Set([...chosen, fresh.trim()])) : chosen;
      await api.put(`/fleet/hosts/${encodeURIComponent(machine!.id)}`,
                    { groups: merged, label, paused });
      onSaved();
    } catch (e) { setError(apiError(e)); } finally { setSaving(false); }
  }

  return (
    <Modal open={!!machine} onClose={onClose} title={machine.hostname || machine.id}
           subtitle={machine.id}>
      {error && <p className="mb-3 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300">{error}</p>}
      <div className="space-y-4">
        <div>
          <Label>Label</Label>
          <Input value={label} onChange={(e) => setLabel(e.target.value)}
                 placeholder="What this machine is, in your words" />
        </div>
        <div>
          <Label>Groups</Label>
          <div className="flex flex-wrap gap-2">
            {all.map((g) => (
              <button key={g} type="button"
                onClick={() => setChosen((c) => c.includes(g) ? c.filter((x) => x !== g) : [...c, g])}
                className={`rounded-full px-2.5 py-1 text-xs ${chosen.includes(g)
                  ? "bg-brand-500/25 text-brand-300" : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700"}`}>
                {g}
              </button>
            ))}
            {all.length === 0 && <span className="text-xs text-zinc-500">No groups yet.</span>}
          </div>
          <Input className="mt-2" value={fresh} onChange={(e) => setFresh(e.target.value)}
                 placeholder="New group name" />
          <p className="mt-1.5 text-xs text-zinc-500">
            Rollouts target groups, and membership is resolved when a machine checks in
            — so a machine added to a group is picked up by a rollout that is already running.
          </p>
        </div>
        <label className="flex items-start gap-2 text-sm text-zinc-300">
          <input type="checkbox" className="mt-0.5" checked={paused}
                 onChange={(e) => setPaused(e.target.checked)} />
          <span>
            Hold this machine back
            <span className="block text-xs text-zinc-500">
              It stays in its groups and keeps reporting, but is never offered an update.
              The rest of the group carries on without waiting for it.
            </span>
          </span>
        </label>
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button loading={saving} onClick={save}>Save</Button>
      </div>
    </Modal>
  );
}
