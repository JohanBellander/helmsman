# ---------- build stage ----------
FROM python:3.12-slim AS build

ENV DEBIAN_FRONTEND=noninteractive
# `apt-get upgrade` here is intentional: the base layer is cached on the
# Coolify build host and lags Debian security updates by days-to-weeks.
# Upgrading inline guarantees every build pulls the freshest libpython3.13
# etc. regardless of base-image age. Costs ~15MB / ~30s per build.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps. --target so we can copy them into the runtime stage without
# also dragging build-time tooling. Console scripts land in /install/bin/.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade "pip>=26.1" \
 && pip install --no-cache-dir --target=/install -r requirements.txt

# Node deps. Two-pass so the generated lockfile honors package.json overrides
# before resolution. npm itself stays in the build stage.
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund --package-lock-only \
 && npm install --omit=dev --no-audit --no-fund

# ---------- runtime stage ----------
FROM python:3.12-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/node_modules/.bin:$PATH

# Node binary only — drop npm and its vendored CVE-bearing deps
# (brace-expansion, glob, minimatch, tar, etc. under
# /usr/lib/node_modules/npm/node_modules/). bridge.py invokes the Coolify
# MCP via `node /app/node_modules/.../dist/index.js`, so npx isn't needed.
# See build stage above — `apt-get upgrade` is here for the same reason:
# the runtime base is cached on the build host and needs an explicit
# refresh to pick up Debian security updates.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get purge -y --auto-remove npm 2>/dev/null || true \
 && rm -rf /usr/lib/node_modules/npm \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

# Upgrade pip in the runtime stage too. The build stage's `pip install
# --upgrade pip` only touches build-side site-packages; pip itself isn't
# in /install (only requirements are), so without this line the runtime
# image keeps the base-layer pip 25.0.1 and Trivy flags it for
# CVE-2025-8869 / CVE-2026-6357 even though we never invoke pip at runtime.
RUN pip install --no-cache-dir --upgrade "pip>=26.1"

WORKDIR /app

# Python packages and their console scripts (e.g. beszel-mcp).
COPY --from=build /install /usr/local/lib/python3.12/site-packages
COPY --from=build /install/bin /usr/local/bin

# Node modules verbatim — overrides preserved, no re-resolution.
COPY --from=build /app/node_modules ./node_modules

COPY bridge.py system_prompt.md ./

EXPOSE 8000

CMD ["python", "bridge.py"]
