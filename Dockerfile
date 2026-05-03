# --- builder: install deps, build CSS ---
FROM python:3.14-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./

RUN uv sync --frozen --no-dev --no-install-project --compile-bytecode

COPY benchlog/ benchlog/

RUN uv sync --frozen --no-dev --compile-bytecode

# Build Tailwind CSS using the standalone CLI (kept only in this stage).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    arch="$(uname -m)" && \
    case "$arch" in \
      x86_64) tw="tailwindcss-linux-x64" ;; \
      aarch64|arm64) tw="tailwindcss-linux-arm64" ;; \
      *) echo "Unsupported arch: $arch" >&2; exit 1 ;; \
    esac && \
    curl -sLO "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/$tw" && \
    chmod +x "$tw" && \
    ./"$tw" -i benchlog/static/css/input.css -o benchlog/static/css/output.css --minify && \
    rm "$tw"

# --- runtime: just python + venv + app + built CSS ---
FROM python:3.14-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/benchlog/ /app/benchlog/
COPY alembic/ alembic/
COPY alembic.ini .

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "benchlog.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers"]
