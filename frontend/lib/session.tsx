"use client";

import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { ApiError, api, setCsrfToken } from "./api";
import type { Session, WorkspaceSummary } from "./types";

type SessionState = {
  status: "loading" | "anonymous" | "authenticated";
  user: Session["user"] | null;
  workspace: WorkspaceSummary | null;
  workspaces: WorkspaceSummary[];
  login: (email: string, password: string) => Promise<void>;
  register: (input: {
    email: string;
    password: string;
    display_name: string;
    workspace_name: string;
  }) => Promise<void>;
  logout: () => Promise<void>;
  selectWorkspace: (workspaceId: string) => void;
  refresh: () => Promise<void>;
};

const SessionContext = createContext<SessionState | null>(null);
const WORKSPACE_KEY = "autoclip.workspace";

export function SessionProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [status, setStatus] = useState<SessionState["status"]>("loading");
  const [user, setUser] = useState<Session["user"] | null>(null);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);

  const workspaces = useMemo(() => user?.workspaces ?? [], [user]);
  const workspace = workspaces.find((w) => w.id === workspaceId) ?? workspaces[0] ?? null;

  const refresh = useCallback(async () => {
    try {
      // `/auth/me` returns the user, not a session envelope: the CSRF token is re-published as a
      // readable cookie by the same response, so it is read from there rather than from a body.
      const user = await api<Session["user"]>("/api/v1/auth/me");
      setUser(user);
      setStatus("authenticated");
      const remembered =
        typeof window === "undefined" ? null : window.localStorage.getItem(WORKSPACE_KEY);
      const valid = user.workspaces.some((w) => w.id === remembered);
      setWorkspaceId(valid ? remembered : (user.workspaces[0]?.id ?? null));
    } catch (error) {
      // 401 here is the ordinary logged-out case, not a failure to report.
      if (!(error instanceof ApiError) || error.status !== 401) {
        console.error("session refresh failed", error);
      }
      setCsrfToken(null);
      setUser(null);
      setStatus("anonymous");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(
    async (email: string, password: string) => {
      const session = await api<Session>("/api/v1/auth/login", {
        method: "POST",
        body: { email, password },
      });
      setCsrfToken(session.csrf_token);
      setUser(session.user);
      setStatus("authenticated");
      setWorkspaceId(session.user.workspaces[0]?.id ?? null);
      router.push("/dashboard");
    },
    [router],
  );

  const register = useCallback(
    async (input: {
      email: string;
      password: string;
      display_name: string;
      workspace_name: string;
    }) => {
      const session = await api<Session>("/api/v1/auth/register", {
        method: "POST",
        body: input,
      });
      setCsrfToken(session.csrf_token);
      setUser(session.user);
      setStatus("authenticated");
      setWorkspaceId(session.user.workspaces[0]?.id ?? null);
      router.push("/dashboard");
    },
    [router],
  );

  const logout = useCallback(async () => {
    await api("/api/v1/auth/logout", { method: "POST" });
    setCsrfToken(null);
    setUser(null);
    setStatus("anonymous");
    router.push("/login");
  }, [router]);

  const selectWorkspace = useCallback((id: string) => {
    window.localStorage.setItem(WORKSPACE_KEY, id);
    setWorkspaceId(id);
  }, []);

  const value = useMemo<SessionState>(
    () => ({
      status,
      user,
      workspace,
      workspaces,
      login,
      register,
      logout,
      selectWorkspace,
      refresh,
    }),
    [status, user, workspace, workspaces, login, register, logout, selectWorkspace, refresh],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside <SessionProvider>");
  return ctx;
}
