"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ApiError } from "@/lib/api";
import { useSession } from "@/lib/session";

export default function LoginPage() {
  const { login, status } = useSession();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (status === "authenticated") router.replace("/dashboard");
  }, [status, router]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email, password);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not sign in.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card auth-card stack" onSubmit={submit}>
      <div>
        <div className="brand" style={{ marginBottom: 14 }}>
          <span className="brand-mark">A</span> AutoClipper
        </div>
        <h1>Sign in</h1>
        <p className="sub" style={{ margin: "4px 0 0" }}>
          Turn a long recording into vertical clips.
        </p>
      </div>

      {error && (
        <div className="banner" data-tone="bad" role="alert">
          {error}
        </div>
      )}

      <div className="field">
        <label className="label" htmlFor="email">
          Email
        </label>
        <input
          id="email"
          className="input"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>

      <div className="field">
        <label className="label" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          className="input"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>

      <button className="btn btn-primary" type="submit" disabled={busy}>
        {busy ? (
          <>
            <span className="spinner" /> Signing in…
          </>
        ) : (
          "Sign in"
        )}
      </button>

      <div className="faint" style={{ textAlign: "center" }}>
        No account?{" "}
        <Link href="/register" style={{ color: "var(--accent)" }}>
          Create one
        </Link>
      </div>
    </form>
  );
}
