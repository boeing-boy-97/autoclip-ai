/** The single place the browser talks to the API.
 *
 *  Every call goes through the Next.js rewrite at /api/v1/*, so the session cookie is first-party
 *  and there is no CORS configuration to get wrong. Mutating calls must carry the CSRF token; the
 *  token is held in memory and re-fetched from /auth/me after a reload, which is why `ensureCsrf`
 *  exists rather than stuffing it in localStorage.
 */

export class ApiError extends Error {
  status: number;
  code: string;
  detail: string;
  fields?: Record<string, string>;

  constructor(status: number, code: string, detail: string, fields?: Record<string, string>) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.fields = fields;
  }
}

/** The CSRF token lives in a readable cookie (`ac_csrf`) because the server keeps only a digest of
 *  it and therefore cannot hand it back on `/auth/me`. Reading the cookie directly is what makes a
 *  page reload survivable; the in-memory copy is just a fast path. */
const CSRF_COOKIE = "ac_csrf";

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function getCsrfToken(): string | null {
  if (csrfToken) return csrfToken;
  if (typeof document === "undefined") return null;
  const match = document.cookie
    .split("; ")
    .find((part) => part.startsWith(`${CSRF_COOKIE}=`));
  if (!match) return null;
  csrfToken = decodeURIComponent(match.slice(CSRF_COOKIE.length + 1));
  return csrfToken;
}

type RequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  /** When set, the body is sent as multipart/form-data (file uploads). */
  form?: FormData;
  signal?: AbortSignal;
};

async function readError(response: Response): Promise<ApiError> {
  // The API returns RFC 9457 problem+json for errors. Falling back to a status line matters:
  // a proxy or crash can return HTML, and the UI must still show something truthful.
  let code = `http_${response.status}`;
  let detail = response.statusText || `request failed with ${response.status}`;
  let fields: Record<string, string> | undefined;
  try {
    const payload = await response.json();
    code = payload.code ?? payload.type ?? code;
    detail = payload.detail ?? payload.title ?? detail;
    if (Array.isArray(payload.errors)) {
      fields = {};
      for (const item of payload.errors) {
        const loc = Array.isArray(item.loc) ? item.loc.filter((p: unknown) => p !== "body") : [];
        if (loc.length) fields[String(loc[loc.length - 1])] = String(item.msg ?? "invalid");
      }
    }
  } catch {
    /* non-JSON error body; keep the status text */
  }
  return new ApiError(response.status, code, detail, fields);
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, form, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") {
    const token = getCsrfToken();
    if (token) headers["X-CSRF-Token"] = token;
  }
  let payload: BodyInit | undefined;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  const response = await fetch(path, {
    method,
    headers,
    body: payload,
    credentials: "same-origin",
    signal,
  });

  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}
