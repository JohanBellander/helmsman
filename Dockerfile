FROM python:3.12-slim

# CVE-2026-6357: upgrade pip. CVE-2025-8869 N/A on Python 3.12 (PEP 706).
RUN pip install --no-cache-dir --upgrade "pip>=26.1"

# Node 20 (for the @masonator/coolify-mcp stdio server). Use NodeSource so we get
# a recent npm without the apt-default's old version. ca-certificates + curl needed
# for the NodeSource setup script.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so they cache across source edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install the Coolify MCP locally so `npx @masonator/coolify-mcp@latest` is fast
# at runtime (no per-spawn registry roundtrip).
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund --package-lock-only && \
    npm install --omit=dev --no-audit --no-fund

# Now the source.
COPY bridge.py system_prompt.md ./

# Webhook port — only exposed on the internal Docker network via docker-compose.
EXPOSE 8000

# Unbuffered logs so docker logs shows things in real time.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["python", "bridge.py"]
