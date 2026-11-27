"use client";

import { useEffect } from "react";

/** A route-level error boundary.
 *
 *  Without this, one unguarded field on one panel replaces the entire app with Next's generic
 *  "client-side exception" page and the user loses the navigation too. There is no way to recover
 *  from that without a reload, which is a bad trade for a rendering mistake in one card.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("route error", error);
  }, [error]);

  return (
    <div className="stack" style={{ gap: 16 }}>
      <h1>Something on this screen failed to render</h1>
      <div className="banner" data-tone="bad" role="alert">
        <div>
          <strong>{error.message || "Unknown error"}</strong>
          {error.digest && (
            <div className="mono faint" style={{ marginTop: 6 }}>
              digest {error.digest}
            </div>
          )}
        </div>
      </div>
      <p className="hint" style={{ margin: 0 }}>
        The rest of the app is still usable — use the navigation on the left, or retry this screen.
      </p>
      <div className="row">
        <button className="btn btn-primary" onClick={reset}>
          Retry
        </button>
        <a className="btn" href="/dashboard">
          Back to dashboard
        </a>
      </div>
    </div>
  );
}
