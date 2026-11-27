import { fileURLToPath } from "node:url";

/** @type {import('next').NextConfig} */
const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  outputFileTracingRoot: fileURLToPath(new URL(".", import.meta.url)),
  // Required by frontend/Dockerfile: the runner stage copies .next/standalone, which Next only
  // emits when standalone output is enabled. Without this the image builds an empty server.
  output: "standalone",
  // The browser only ever talks to this origin. Everything under /api/v1 is proxied to the API,
  // which keeps the session cookie first-party and removes CORS from the auth story entirely.
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${API_INTERNAL_URL}/api/v1/:path*` }];
  },
  // Large uploads go through the proxy too; the default body limit is too small for a video.
  experimental: { proxyTimeout: 600_000 },
};

export default nextConfig;
