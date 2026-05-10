# ---------- build stage ----------
FROM python:3.12-slim AS build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
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
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get purge -y --auto-remove npm 2>/dev/null || true \
 && rm -rf /usr/lib/node_modules/npm \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python packages and their console scripts (e.g. beszel-mcp).
COPY --from=build /install /usr/local/lib/python3.12/site-packages
COPY --from=build /install/bin /usr/local/bin

# Node modules verbatim — overrides preserved, no re-resolution.
COPY --from=build /app/node_modules ./node_modules

COPY bridge.py system_prompt.md ./

EXPOSE 8000

CMD ["python", "bridge.py"]
