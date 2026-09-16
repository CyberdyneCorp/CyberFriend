/**
 * The shell: sign in, or the screens.
 *
 * Routing is by hash. A path router would need the API to rewrite every
 * unknown path to index.html, and a missing rewrite fails as a 404 on
 * refresh, days after the deploy that caused it. The hash router asks nothing
 * of the server beyond serving files, which is the whole of what the API
 * promises to do with this bundle.
 */

import { HashRouter, NavLink, Navigate, Route, Routes } from "react-router-dom";

import { signOut } from "./auth/session";
import { useSignedIn } from "./hooks/useSession";
import { AuditScreen } from "./screens/AuditScreen";
import { ChannelsScreen } from "./screens/ChannelsScreen";
import { FederationScreen } from "./screens/FederationScreen";
import { RetentionScreen } from "./screens/RetentionScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { SignIn } from "./screens/SignIn";
import { StatusScreen } from "./screens/StatusScreen";
import { TokensScreen } from "./screens/TokensScreen";

const SCREENS: { path: string; title: string; element: JSX.Element }[] = [
  { path: "/status", title: "Status", element: <StatusScreen /> },
  { path: "/federation", title: "Federation", element: <FederationScreen /> },
  { path: "/channels", title: "Channels", element: <ChannelsScreen /> },
  { path: "/retention", title: "Retention", element: <RetentionScreen /> },
  { path: "/settings", title: "Settings", element: <SettingsScreen /> },
  { path: "/tokens", title: "Tokens", element: <TokensScreen /> },
  { path: "/audit", title: "Audit", element: <AuditScreen /> },
];

function Shell() {
  return (
    <div className="shell">
      <nav className="nav">
        <div className="nav__brand">CyberFriend</div>
        {SCREENS.map((screen) => (
          <NavLink
            key={screen.path}
            to={screen.path}
            className={({ isActive }) => (isActive ? "nav__link nav__link--on" : "nav__link")}
          >
            {screen.title}
          </NavLink>
        ))}
        <button type="button" className="button button--quiet nav__out" onClick={signOut}>
          Sign out
        </button>
        <p className="nav__note">
          Your token is in this tab's memory only. Closing or reloading it signs
          you out.
        </p>
      </nav>
      <main className="main">
        <Routes>
          {SCREENS.map((screen) => (
            <Route key={screen.path} path={screen.path} element={screen.element} />
          ))}
          <Route path="*" element={<Navigate to="/status" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export function App() {
  // Signing out unmounts every screen, so nothing keeps rendering data
  // fetched with a credential that is gone.
  return <HashRouter>{useSignedIn() ? <Shell /> : <SignIn />}</HashRouter>;
}
