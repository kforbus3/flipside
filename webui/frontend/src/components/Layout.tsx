import { ReactNode, useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { LayoutDashboard, Hammer, HardDrive, ListChecks, Network, LogOut, Boxes, MonitorSmartphone, Server, Package, Rocket, KeyRound, FolderCog, Users, KeySquare, Fingerprint, ScrollText, Archive } from "lucide-react";
import { useAuth, roleAtLeast, Role } from "../lib/auth";
import { api } from "../lib/api";

// `min` is the role the page's API surface requires; entries above the
// signed-in role are not offered rather than offered and refused.
const NAV: { to: string; label: string; icon: any; min: Role }[] = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, min: "viewer" },
  { to: "/build", label: "Build Image", icon: Hammer, min: "viewer" },
  { to: "/images", label: "Images", icon: HardDrive, min: "viewer" },
  { to: "/files", label: "Image Files", icon: FolderCog, min: "viewer" },
  { to: "/jobs", label: "Jobs", icon: ListChecks, min: "viewer" },
  { to: "/provisioning", label: "Provisioning", icon: Network, min: "viewer" },
  { to: "/imaging", label: "Imaging", icon: MonitorSmartphone, min: "viewer" },
  { to: "/fleet", label: "Fleet", icon: Server, min: "viewer" },
  { to: "/updates", label: "Updates", icon: Package, min: "viewer" },
  { to: "/rollouts", label: "Rollouts", icon: Rocket, min: "viewer" },
  { to: "/secrets", label: "Secrets Manager", icon: KeyRound, min: "admin" },
];

const ADMIN_NAV: { to: string; label: string; icon: any }[] = [
  { to: "/users", label: "Users", icon: Users },
  { to: "/tokens", label: "API Tokens", icon: KeySquare },
  { to: "/sessions", label: "Sessions", icon: Fingerprint },
  { to: "/audit", label: "Audit Log", icon: ScrollText },
  { to: "/backup", label: "Backup", icon: Archive },
];

export default function Layout({ children }: { children: ReactNode }) {
  const { logout, username, role, isAdmin } = useAuth();
  const navigate = useNavigate();

  // Machines being imaged are the one thing happening away from the screen, so
  // the count follows you across every page rather than only the Imaging one.
  const [imaging, setImaging] = useState(0);
  useEffect(() => {
    const poll = () => api.get("/imaging").then((r) => setImaging(r.data.active)).catch(() => {});
    poll();
    const t = setInterval(poll, 5000);
    return () => clearInterval(t);
  }, []);

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition ${isActive ? "bg-brand-600/20 text-brand-400" : "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"}`;

  return (
    <div className="flex h-full">
      <aside className="flex w-56 shrink-0 flex-col border-r border-zinc-800 bg-zinc-900/40">
        <div className="flex items-center gap-2 px-5 py-4">
          <Boxes className="text-brand-400" /><span className="font-semibold tracking-tight">Flipside</span>
        </div>
        <nav className="flex-1 space-y-0.5 overflow-y-auto px-3 py-2">
          {NAV.filter((n) => roleAtLeast(role, n.min)).map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"} className={linkClass}>
              <n.icon size={17} />{n.label}
              {n.to === "/imaging" && imaging > 0 && (
                <span className="ml-auto inline-flex min-w-[1.25rem] justify-center rounded-full bg-brand-500/20 px-1.5 py-0.5 text-xs font-medium tabular-nums text-brand-300">
                  {imaging}
                </span>
              )}
            </NavLink>
          ))}
          {isAdmin && (
            <>
              <p className="px-3 pb-1 pt-4 text-[11px] font-medium uppercase tracking-wide text-zinc-600">Access</p>
              {ADMIN_NAV.map((n) => (
                <NavLink key={n.to} to={n.to} className={linkClass}>
                  <n.icon size={17} />{n.label}
                </NavLink>
              ))}
            </>
          )}
        </nav>
        <div className="border-t border-zinc-800 px-5 pt-3 text-xs">
          <p className="truncate font-medium text-zinc-300">{username}</p>
          <p className="text-zinc-500">{role}</p>
        </div>
        <button onClick={() => { logout(); navigate("/login"); }}
          className="m-3 flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-zinc-400 hover:bg-zinc-800 hover:text-red-300">
          <LogOut size={17} /> Log out
        </button>
      </aside>
      <main className="flex-1 overflow-y-auto px-6 py-6">{children}</main>
    </div>
  );
}
