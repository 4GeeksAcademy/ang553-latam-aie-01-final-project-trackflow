FROM node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a AS base

WORKDIR /app

# ---------------------------------------------------------------
# Dependency installation layer (leverages Docker cache)
# ---------------------------------------------------------------
COPY uis/website/package.json uis/website/package-lock.json ./uis/website/
COPY uis/backoffice/package.json uis/backoffice/package-lock.json ./uis/backoffice/

RUN cd /app/uis/website && npm ci
RUN cd /app/uis/backoffice && npm ci

# ---------------------------------------------------------------
# Source code layer
# ---------------------------------------------------------------
COPY uis/website/ ./uis/website/
COPY uis/backoffice/ ./uis/backoffice/
COPY src/ ./src/

ARG API_BACKEND_URL=http://api:8000
# Next.js inlines public browser configuration at build time.
ARG NEXT_PUBLIC_SUPPORT_OIDC_AUTHORIZATION_ENDPOINT
ARG NEXT_PUBLIC_SUPPORT_OIDC_TOKEN_ENDPOINT
ARG NEXT_PUBLIC_SUPPORT_OIDC_CLIENT_ID
ARG NEXT_PUBLIC_TELEMETRY_ENDPOINT
ARG NEXT_PUBLIC_CHAT_WS_URL
ENV API_BACKEND_URL=${API_BACKEND_URL}
RUN cd /app/uis/backoffice && npm run build

# ---------------------------------------------------------------
# Start script
# ---------------------------------------------------------------
COPY uis/start.sh /uis/start.sh
RUN sed -i 's/\r$//' /uis/start.sh && chmod 0555 /uis/start.sh

# ---------------------------------------------------------------
# Ports
# ---------------------------------------------------------------
EXPOSE 3000 3001

# ---------------------------------------------------------------
# Runtime targets
# ---------------------------------------------------------------
FROM base AS production
RUN cd /app/uis/website && npm run build \
    && mkdir -p /app/uis/website/.next/cache /app/uis/backoffice/.next/cache \
    && chown -R node:node /app/uis/website/.next/cache /app/uis/backoffice/.next/cache
USER node:node
CMD ["/uis/start.sh", "start"]

# Preserve the default image used by Compose for Website hot reload.
FROM base AS development
RUN mkdir -p /app/uis/website/.next /app/uis/backoffice/.next/cache \
    && touch /app/uis/website/next-env.d.ts /app/uis/website/AGENTS.md \
    && chown -R node:node /app/uis/website/.next /app/uis/backoffice/.next/cache \
    && chown node:node /app/uis/website/next-env.d.ts /app/uis/website/AGENTS.md \
    && chmod -R g+rwX /app/uis/backoffice/.next/cache
USER node:node
CMD ["/uis/start.sh"]
