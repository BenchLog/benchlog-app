FROM python:3.14-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev

COPY benchlog/ benchlog/
COPY alembic/ alembic/
COPY alembic.ini .

# Download Tailwind standalone CLI
RUN apt-get update && apt-get install -y curl && \
    curl -sLO https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-arm64 && \
    chmod +x tailwindcss-linux-arm64 && \
    mv tailwindcss-linux-arm64 /usr/local/bin/tailwindcss && \
    apt-get remove -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Build Tailwind CSS
RUN tailwindcss -i benchlog/static/css/input.css -o benchlog/static/css/output.css --minify

RUN mkdir -p /app/data/files

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "benchlog.main:app", "--host", "0.0.0.0", "--port", "8000"]
