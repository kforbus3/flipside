import { Routes, Route, Navigate } from "react-router-dom";
import { useAuth } from "./lib/auth";
import Layout from "./components/Layout";
import { Spinner } from "./components/ui";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Build from "./pages/Build";
import Images from "./pages/Images";
import Files from "./pages/Files";
import Jobs from "./pages/Jobs";
import Provisioning from "./pages/Provisioning";
import Imaging from "./pages/Imaging";
import Fleet from "./pages/Fleet";
import Updates from "./pages/Updates";
import Rollouts from "./pages/Rollouts";
import Secrets from "./pages/Secrets";
import UsersPage from "./pages/Users";
import Tokens from "./pages/Tokens";
import Sessions from "./pages/Sessions";
import Audit from "./pages/Audit";

function Protected({ children, admin }: { children: JSX.Element; admin?: boolean }) {
  const { authed, loading, isAdmin } = useAuth();
  if (loading) return <Spinner />;
  if (!authed) return <Navigate to="/login" replace />;
  // The backend answers 403 regardless; this just spares a non-admin a page
  // of failed requests when they type the path by hand.
  if (admin && !isAdmin) return <Navigate to="/" replace />;
  return <Layout>{children}</Layout>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<Protected><Dashboard /></Protected>} />
      <Route path="/build" element={<Protected><Build /></Protected>} />
      <Route path="/images" element={<Protected><Images /></Protected>} />
      <Route path="/files" element={<Protected><Files /></Protected>} />
      <Route path="/jobs" element={<Protected><Jobs /></Protected>} />
      <Route path="/provisioning" element={<Protected><Provisioning /></Protected>} />
      <Route path="/imaging" element={<Protected><Imaging /></Protected>} />
      <Route path="/fleet" element={<Protected><Fleet /></Protected>} />
      <Route path="/updates" element={<Protected><Updates /></Protected>} />
      <Route path="/rollouts" element={<Protected><Rollouts /></Protected>} />
      <Route path="/secrets" element={<Protected admin><Secrets /></Protected>} />
      <Route path="/users" element={<Protected admin><UsersPage /></Protected>} />
      <Route path="/tokens" element={<Protected admin><Tokens /></Protected>} />
      <Route path="/sessions" element={<Protected admin><Sessions /></Protected>} />
      <Route path="/audit" element={<Protected admin><Audit /></Protected>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
