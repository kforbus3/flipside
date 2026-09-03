import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { HardDrive, Cpu, Network, Hammer, Database, MonitorSmartphone, Server, Rocket, AlertTriangle } from "lucide-react";
import { api, fmtBytes } from "../lib/api";
import { Card, PageHeader, Badge, Alert } from "../components/ui";

export default function Dashboard() {
  const [images, setImages] = useState<any[]>([]);
  const [imagerReady, setImagerReady] = useState(false);
  const [server, setServer] = useState<{ running: boolean } | null>(null);
  const [disk, setDisk] = useState<{ artifacts: number; free: number } | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [imaging, setImaging] = useState(0);
  const [fleet, setFleet] = useState<any>(null);

  useEffect(() => {
    api.get("/preflight").then((r) => setProblems(r.data.problems)).catch(() => {});
    api.get("/images").then((r) => { setImages(r.data.images); setImagerReady(r.data.imager_ready); }).catch(() => {});
    api.get("/server/status").then((r) => setServer(r.data)).catch(() => setServer({ running: false }));
    api.get("/images/disk").then((r) => setDisk(r.data)).catch(() => {});

    // The count of machines imaging right now is the one number on this page
    // that changes on its own, so it is the only one worth polling.
    // The two numbers on this page that change on their own: machines writing
    // an image right now, and the fleet's own state. Everything else here is
    // static until somebody does something.
    const poll = () => {
      api.get("/imaging").then((r) => setImaging(r.data.active)).catch(() => {});
      api.get("/metrics.json").then((r) => setFleet(r.data)).catch(() => {});
    };
    poll();
    const t = setInterval(poll, 15000);
    return () => clearInterval(t);
  }, []);

  return (
    <div>
      <PageHeader title="Dashboard" subtitle="Build images and provision machines over the network" />
      <Alert title="Setup needed before builds will run" items={problems} />
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card className="p-5">
          <div className="flex items-center justify-between"><HardDrive className="text-brand-400" size={22} /><span className="text-3xl font-semibold">{images.length}</span></div>
          <p className="mt-2 text-sm text-zinc-400">Built images</p>
        </Card>
        <Card className="p-5">
          <div className="flex items-center justify-between"><Cpu className="text-sky-400" size={22} />{imagerReady ? <Badge color="green">ready</Badge> : <Badge color="amber">not built</Badge>}</div>
          <p className="mt-2 text-sm text-zinc-400">Netboot imager</p>
        </Card>
        <Card className="p-5">
          <div className="flex items-center justify-between"><Network className="text-emerald-400" size={22} />{server?.running ? <Badge color="green">running</Badge> : <Badge color="zinc">stopped</Badge>}</div>
          <p className="mt-2 text-sm text-zinc-400">Provisioning server</p>
        </Card>
        <Card className="p-5">
          <div className="flex items-center justify-between"><Database className="text-amber-400" size={22} />
            <span className="text-right text-sm font-semibold">{disk ? fmtBytes(disk.artifacts) : "—"}</span>
          </div>
          <p className="mt-2 text-sm text-zinc-400">Artifacts on disk{disk && <span className="text-zinc-500"> · {fmtBytes(disk.free)} free</span>}</p>
        </Card>
      </div>
      {/* The fleet, but only once there is one. A dashboard that shows four
          zeroes on a server nobody has provisioned from yet is worse than one
          that says nothing — and the interesting numbers here are the ones
          that mean something is wrong. */}
      {fleet && fleet.fleet.machines > 0 && (
        <div className="mt-4 grid grid-cols-2 gap-4 md:grid-cols-4">
          <Link to="/fleet"><Card className="h-full p-5 hover:border-brand-500/50">
            <div className="flex items-center justify-between">
              <Server className="text-emerald-400" size={22} />
              <span className="text-3xl font-semibold tabular-nums">
                {fleet.fleet.presence.online}
              </span>
            </div>
            <p className="mt-2 text-sm text-zinc-400">
              Machines online
              <span className="text-zinc-500"> · {fleet.fleet.machines} known</span>
            </p>
          </Card></Link>

          <Link to="/fleet"><Card className={`h-full p-5 hover:border-brand-500/50 ${
            fleet.fleet.presence.offline > 0 ? "border-red-500/40" : ""}`}>
            <div className="flex items-center justify-between">
              <AlertTriangle className={fleet.fleet.presence.offline > 0
                ? "text-red-400" : "text-zinc-600"} size={22} />
              <span className="text-3xl font-semibold tabular-nums">
                {fleet.fleet.presence.offline + fleet.fleet.presence.stale}
              </span>
            </div>
            <p className="mt-2 text-sm text-zinc-400">
              Not checking in
              {fleet.fleet.degraded > 0 &&
                <span className="text-red-400"> · {fleet.fleet.degraded} degraded</span>}
            </p>
          </Card></Link>

          <Link to="/rollouts"><Card className={`h-full p-5 hover:border-brand-500/50 ${
            fleet.rollouts.halted > 0 ? "border-red-500/40" : ""}`}>
            <div className="flex items-center justify-between">
              <Rocket className={fleet.rollouts.halted > 0 ? "text-red-400" : "text-sky-400"} size={22} />
              <span className="text-3xl font-semibold tabular-nums">{fleet.rollouts.live}</span>
            </div>
            <p className="mt-2 text-sm text-zinc-400">
              Live rollouts
              {fleet.rollouts.halted > 0 &&
                <span className="text-red-400"> · {fleet.rollouts.halted} halted</span>}
            </p>
          </Card></Link>

          <Link to="/fleet"><Card className={`h-full p-5 hover:border-brand-500/50 ${
            fleet.fleet.never_booted > 0 ? "border-amber-500/40" : ""}`}>
            <div className="flex items-center justify-between">
              <MonitorSmartphone className={fleet.fleet.never_booted > 0
                ? "text-amber-400" : "text-zinc-600"} size={22} />
              <span className="text-3xl font-semibold tabular-nums">
                {fleet.fleet.never_booted}
              </span>
            </div>
            {/* The imager's own reports cannot cover this: the last one is sent
                before the reboot, so a machine that images perfectly and then
                fails to boot looks exactly like a success. */}
            <p className="mt-2 text-sm text-zinc-400">Imaged, never booted</p>
          </Card></Link>
        </div>
      )}

      <div className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-3">
        <Link to="/build"><Card className="flex h-full items-center gap-3 p-5 hover:border-brand-500/50"><Hammer className="shrink-0 text-brand-400" /><div><p className="font-medium">Build a new image</p><p className="text-xs text-zinc-500">Configure and watch the build live</p></div></Card></Link>
        <Link to="/provisioning"><Card className="flex h-full items-center gap-3 p-5 hover:border-brand-500/50"><Network className="shrink-0 text-emerald-400" /><div><p className="font-medium">Provision machines</p><p className="text-xs text-zinc-500">Start the PXE server and hand out images</p></div></Card></Link>
        <Link to="/imaging"><Card className="flex h-full items-center gap-3 p-5 hover:border-brand-500/50"><MonitorSmartphone className="shrink-0 text-sky-400" /><div><p className="font-medium">Imaging now</p><p className="text-xs text-zinc-500">{imaging > 0 ? `${imaging} machine${imaging === 1 ? "" : "s"} writing an image` : "No machines are imaging"}</p></div>{imaging > 0 && <Badge color="blue">{imaging}</Badge>}</Card></Link>
      </div>
    </div>
  );
}
