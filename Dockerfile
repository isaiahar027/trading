# Odds Decision Hub: read-only odds analytics dashboard.
# It only talks to The Odds API (and optional notification webhooks). No browser, no sportsbook automation.
#
#   docker compose up -d --build      (recommended, see docker-compose.yml)
#   docker build -t odds-hub .        (plain Docker)

# ---------------------------------------------------------------------------------------------------------------
# Build stage: install all dependencies, compile TypeScript to dist/, then drop dev dependencies.
# ---------------------------------------------------------------------------------------------------------------
FROM node:22-bookworm-slim AS build
WORKDIR /app

# Dependencies first so this layer is cached until package*.json changes.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY tsconfig.json ./
COPY src ./src
# "build" is `tsc -p tsconfig.json`: src/**/*.ts -> dist/ (entry point dist/index.js).
RUN npm run build \
 && test -f dist/index.js

# Keep only production dependencies for the runtime image (mkdir: node_modules may be empty after pruning).
RUN npm prune --omit=dev --no-audit --no-fund \
 && mkdir -p node_modules

# ---------------------------------------------------------------------------------------------------------------
# Runtime stage: compiled JS + static dashboard, running as the unprivileged `node` user (uid 1000).
# ---------------------------------------------------------------------------------------------------------------
FROM node:22-bookworm-slim AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/app/data

# Application files stay owned by root (read-only for the app); only the data directory is writable.
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY public ./public

# DATA_DIR holds settings.json and the bet journal. Mount a volume here (docker-compose.yml mounts ./data).
RUN mkdir -p /app/data \
 && chown node:node /app/data

USER node

EXPOSE 8080

# /healthz is the only unauthenticated route, so the check works with DASHBOARD_PASSWORD set.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:' + (process.env.PORT || '8080') + '/healthz', { signal: AbortSignal.timeout(4000) }).then((r) => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]

CMD ["node", "dist/index.js"]
