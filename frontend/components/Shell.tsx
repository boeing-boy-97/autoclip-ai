"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useSession } from "@/lib/session";

const NAV = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/projects", label: "Projects" },
  { href: "/jobs", label: "Jobs" },
];

const PUBLIC_PATHS = ["/login", "/register"];

export function Shell({ children }: { children: React.ReactNode }) {
  const { status, error: sessionError, refresh, workspace, workspaces, logout, selectWorkspace } = useSession();
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const pathname = usePathname();
  const router = useRouter();
  const isPublic = PUBLIC_PATHS.includes(pathname);

  useEffect(() => {
    if (status === "anonymous" && !isPublic) router.replace("/login");
    if (status === "authenticated" && isPublic) router.replace("/dashboard");
  }, [status, isPublic, router]);

  if (isPublic) return <div className="centre-screen">{children}</div>;

  if (status === "loading") {
    return (
      <div className="centre-screen">
        <div className="row dim">
          <span className="spinner" /> Loading your workspace…
        </div>
      </div>
    );
  }

  if (status === "error") {
    return <div className="centre-screen"><div className="card stack" role="alert">
      <p>{sessionError}</p>
      <button className="btn" onClick={() => void refresh()}>Retry connection</button>
    </div></div>;
  }

  if (status === "anonymous") {
    return (
      <div className="centre-screen">
        <div className="row dim">
          <span className="spinner" /> Redirecting to sign in…
        </div>
      </div>
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <Link href="/dashboard" className="brand">
          <span className="brand-mark">A</span> AutoClipper
        </Link>

        <nav className="nav" aria-label="Main navigation">
          {NAV.map((item) => (
            <Link
              aria-current={pathname === item.href ? "page" : undefined}
              key={item.href}
              href={item.href}
              data-active={pathname === item.href || pathname.startsWith(`${item.href}/`)}
            >
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="sidebar-foot">
          {workspaces.length > 1 ? (
            <select
              className="select"
              value={workspace?.id ?? ""}
              onChange={(event) => selectWorkspace(event.target.value)}
              aria-label="Workspace"
            >
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </select>
          ) : (
            <div className="faint truncate">{workspace?.name}</div>
          )}
          {logoutError && <div role="alert" className="error-text">{logoutError}</div>}
          <button className="btn btn-sm" disabled={signingOut} onClick={async () => {
            setSigningOut(true);
            setLogoutError(null);
            try { await logout(); }
            catch { setLogoutError("Sign out failed. Please try again."); }
            finally { setSigningOut(false); }
          }}>
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
        </div>
      </aside>

      <main className="main" key={`${workspace?.id}:${pathname}`}>{children}</main>
    </div>
  );
}
