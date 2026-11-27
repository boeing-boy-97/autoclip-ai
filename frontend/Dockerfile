# syntax=docker/dockerfile:1.7
# =====================================================================================
# Frontend image: Next.js standalone output, built separately from the API.
#
# Three things make this image work and are easy to break:
#   1. `output: "standalone"` in next.config.mjs — without it there is no .next/standalone
#      directory to copy and the runner stage has nothing to start.
#   2. `npm ci` (dev dependencies included) in the builder — `next build` needs `typescript`
#      and `@types/*`, which are devDependencies.
#   3. public/ must exist in the build context. It is committed with a .gitkeep.
# =====================================================================================
FROM node:20-alpine AS deps
WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY frontend/package.json frontend/package-lock.json* ./
# Full install: the builder needs typescript and type packages to compile the app.
RUN npm ci --no-audit --no-fund

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ ./
ARG API_INTERNAL_URL=http://api:8000
ENV NEXT_TELEMETRY_DISABLED=1 API_INTERNAL_URL=$API_INTERNAL_URL
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -g 10001 nodejs && adduser -S -u 10001 -G nodejs nextjs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.js"]
