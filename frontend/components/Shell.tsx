"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { useSession } from "@/lib/session";

const NAV = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/projects", label: "Projects" },
  { href: "/jobs", label: "Jobs" },
];

const PUBLIC_PATHS = ["/login", "/register"];

export function Shell({ children }: { children: React.ReactNode }) {
  const { status, workspace, workspaces, logout, selectWorkspace } = useSession();
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

        <nav className="nav">
          {NAV.map((item) => (
            <Link
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
          <button className="btn btn-sm" onClick={() => void logout()}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">{children}</main>
    </div>
  );
}
