FROM node:20-alpine AS base

# Local TLS-interception roots (corporate proxy or antivirus that re-signs HTTPS).
# certs/ ships empty, so all of this is a no-op on a normal network. Without it
# apk, corepack (fetching pnpm) and pnpm all fail on certificate verification.
#
# Order matters and is not obvious: apk needs the cert to reach its own package
# index, so the bundle is seeded by hand BEFORE any apk call. Installing
# ca-certificates first cannot work - that install is itself the thing that
# needs the cert.
COPY certs/ /usr/local/share/ca-certificates/local/
RUN set -eu; \
    if ls /usr/local/share/ca-certificates/local/*.crt >/dev/null 2>&1; then \
      cat /usr/local/share/ca-certificates/local/*.crt >> /etc/ssl/certs/ca-certificates.crt; \
    fi
RUN apk add --no-cache libc6-compat ca-certificates
# Two things that cost a build round each, worth stating:
#
# 1. `update-ca-certificates` regenerates the bundle from scratch and does NOT
#    pick up certs in a subdirectory of /usr/local/share/ca-certificates, so it
#    silently drops what the seeding step above added. Measured: bundle went to
#    119 certs with the local root gone. Hence the re-append afterwards.
# 2. Node ignores the system trust store entirely. NODE_EXTRA_CA_CERTS must
#    point at a real, non-empty PEM, so when no local cert was supplied it is
#    pointed at a copy of the system bundle rather than an empty file - an empty
#    one makes Node warn on every invocation.
RUN set -eu; \
    update-ca-certificates || true; \
    if ls /usr/local/share/ca-certificates/local/*.crt >/dev/null 2>&1; then \
      cat /usr/local/share/ca-certificates/local/*.crt >> /etc/ssl/certs/ca-certificates.crt; \
      cat /usr/local/share/ca-certificates/local/*.crt > /etc/ssl/certs/local-extra.pem; \
    else \
      cp /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/local-extra.pem; \
    fi
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/local-extra.pem

WORKDIR /app

# Dependencies
FROM base AS deps
COPY package.json yarn.lock* package-lock.json* pnpm-lock.yaml* ./
RUN \
  if [ -f yarn.lock ]; then yarn --frozen-lockfile --network-timeout 600000; \
  elif [ -f package-lock.json ]; then npm ci; \
  elif [ -f pnpm-lock.yaml ]; then corepack enable pnpm && pnpm i; \
  else npm install; \
  fi

# Build
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ENV NEXT_TELEMETRY_DISABLED=1
# Placeholder values replaced at runtime by entrypoint.sh
ENV NEXT_PUBLIC_API_URL=NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_INSTANCE_NAME=NEXT_PUBLIC_INSTANCE_NAME

RUN npm run build

# Production
FROM base AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --chown=nextjs:nodejs entrypoint.sh /home/nextjs/entrypoint.sh
RUN chmod +x /home/nextjs/entrypoint.sh

USER nextjs

EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

ENTRYPOINT ["/home/nextjs/entrypoint.sh"]
CMD ["node", "server.js"]
