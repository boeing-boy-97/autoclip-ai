# AutoClipper web

Next.js 15 (App Router), TypeScript strict, no UI framework.

## Run against a local API

```bash
npm ci
API_INTERNAL_URL=http://127.0.0.1:8000 npm run dev     # development
API_INTERNAL_URL=http://127.0.0.1:8000 npm run build && npm start
```

`API_INTERNAL_URL` is server-side only. The browser never learns the API's address: `next.config.mjs`
rewrites `/api/v1/*` to it, so the session cookie is first-party and there is no CORS configuration
to get wrong.

## The one thing to know

**The CSRF token comes from a cookie, not from a response body.** The API stores only a digest of the
token, so it cannot hand the token back on `/auth/me`. It publishes it as `ac_csrf`, which is
readable by design (so a reloaded page can still mutate) and is never trusted on its own — it is
only ever compared against the `X-CSRF-Token` header and the stored digest. `getCsrfToken()` in
`lib/api.ts` reads it.

## Layout

```
app/            routes; error.tsx is a route-level boundary
components/     Shell (nav + session gate), ClipCard, ui primitives
lib/api.ts      the single fetch wrapper, incl. problem+json error parsing
lib/session.tsx session context: who am I, which workspace, login/logout
lib/types.ts    wire types, hand-written from real responses
```

`lib/types.ts` is deliberately hand-written rather than generated. Two crashes came from guessing a
response shape; when a type is added here it should be copied from a real response body first.
