"use client";

import Link from "next/link";
import { useState } from "react";

import { ApiError } from "@/lib/api";
import { useSession } from "@/lib/session";

export default function RegisterPage() {
  const { register } = useSession();
  const [form, setForm] = useState({
    email: "",
    password: "",
    display_name: "",
    workspace_name: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setFieldErrors({});
    setBusy(true);
    try {
      await register(form);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.detail);
        setFieldErrors(err.fields ?? {});
      } else {
        setError("Could not create the account.");
      }
    } finally {
      setBusy(false);
    }
  }

  const set =
    (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((prev) => ({ ...prev, [key]: e.target.value }));

  return (
    <form className="card auth-card stack" onSubmit={submit}>
      <div>
        <div className="brand" style={{ marginBottom: 14 }}>
          <span className="brand-mark">A</span> AutoClipper
        </div>
        <h1>Create an account</h1>
        <p className="sub" style={{ margin: "4px 0 0" }}>
          You get a workspace to keep your projects in.
        </p>
      </div>

      {error && (
        <div className="banner" data-tone="bad" role="alert">
          {error}
        </div>
      )}

      <div className="field">
        <label className="label" htmlFor="display_name">
          Your name
        </label>
        <input
          id="display_name"
          className="input"
          required
          value={form.display_name}
          onChange={set("display_name")}
        />
        {fieldErrors.display_name && (
          <span className="error-text">{fieldErrors.display_name}</span>
        )}
      </div>

      <div className="field">
        <label className="label" htmlFor="workspace_name">
          Workspace name
        </label>
        <input
          id="workspace_name"
          className="input"
          required
          value={form.workspace_name}
          onChange={set("workspace_name")}
        />
        {fieldErrors.workspace_name && (
          <span className="error-text">{fieldErrors.workspace_name}</span>
        )}
      </div>

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
          value={form.email}
          onChange={set("email")}
        />
        {fieldErrors.email && <span className="error-text">{fieldErrors.email}</span>}
      </div>

      <div className="field">
        <label className="label" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          className="input"
          type="password"
          autoComplete="new-password"
          required
          value={form.password}
          onChange={set("password")}
        />
        <span className="hint">At least 12 characters. Length beats complexity.</span>
        {fieldErrors.password && <span className="error-text">{fieldErrors.password}</span>}
      </div>

      <button className="btn btn-primary" type="submit" disabled={busy}>
        {busy ? (
          <>
            <span className="spinner" /> Creating…
          </>
        ) : (
          "Create account"
        )}
      </button>

      <div className="faint" style={{ textAlign: "center" }}>
        Already have one?{" "}
        <Link href="/login" style={{ color: "var(--accent)" }}>
          Sign in
        </Link>
      </div>
    </form>
  );
}
