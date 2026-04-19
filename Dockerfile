FROM python:3.14-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev

COPY benchlog/ benchlog/
COPY alembic/ alembic/
COPY alembic.ini .

# Download Tailwind standalone CLI (auto-detect x64 / arm64).
RUN apt-get update && apt-get install -y curl && \
    arch="$(uname -m)" && \
    case "$arch" in \
      x86_64) tw="tailwindcss-linux-x64" ;; \
      aarch64|arm64) tw="tailwindcss-linux-arm64" ;; \
      *) echo "Unsupported arch: $arch" >&2; exit 1 ;; \
    esac && \
    curl -sLO "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/$tw" && \
    chmod +x "$tw" && mv "$tw" /usr/local/bin/tailwindcss && \
    apt-get remove -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Build Tailwind CSS
RUN tailwindcss -i benchlog/static/css/input.css -o benchlog/static/css/output.css --minify

RUN mkdir -p /app/data/files

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "benchlog.main:app", "--host", "0.0.0.0", "--port", "8000"]
