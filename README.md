# BenchLog

**Very much a WIP POC/Concept phase right now to help flesh out this idea before building this for real**

A self-hosted project journal for makers. Document, archive, and organize your maker projects — 3D printing, electronics, CNC, woodworking, and everything in between.

## Features

- **Project management** — Create projects with markdown descriptions, status tracking, tags, and cover images
- **File browser** — Upload, organize, and version files with folder navigation, drag-and-drop, and batch operations
- **Build updates** — Post micro-updates or long-form blog posts with markdown support
- **Bill of materials** — Track parts, quantities, suppliers, and costs
- **External links** — Organize GitHub repos, videos, docs, and other resources per project
- **Tags** — Color-coded tags for filtering and organizing projects
- **Search** — Full-text search across projects, files, and updates

## Tech Stack

- **Backend:** Python 3.14, FastAPI, SQLAlchemy 2.0 (async), Alembic
- **Database:** PostgreSQL 18
- **Frontend:** Jinja2 templates, HTMX, Alpine.js, Tailwind CSS (standalone CLI)
- **Storage:** Local filesystem (S3-compatible abstraction for future use)
- **Package manager:** uv

## Quick Start

### Prerequisites

- [uv](https://docs.astral.sh/uv/) for Python package management
- [Docker](https://www.docker.com/) for PostgreSQL (or a local Postgres instance)

### Development Setup

```bash
# Start PostgreSQL
docker compose up db -d

# Install dependencies
uv sync

# Run database migrations
uv run alembic upgrade head

# Start the dev server
uv run uvicorn benchlog.main:app --reload

# In another terminal, watch Tailwind CSS for changes
./tailwindcss -i benchlog/static/css/input.css -o benchlog/static/css/output.css --watch
```

The app will be available at `http://localhost:8000`. Default login is `admin` / `admin` (configurable via environment variables).

### Docker Compose (Full Stack)

```bash
docker compose up
```

This starts both PostgreSQL and the app server.

## Configuration

All settings are configured via environment variables prefixed with `BENCHLOG_`:

| Variable | Default | Description |
| --- | --- | --- |
| `BENCHLOG_SECRET_KEY` | `change-me` | Session signing key |
| `BENCHLOG_DATABASE_URL` | `postgresql+asyncpg://benchlog:benchlog@localhost/benchlog` | Database connection |
| `BENCHLOG_USERNAME` | `admin` | Login username |
| `BENCHLOG_PASSWORD` | `admin` | Login password |
| `BENCHLOG_STORAGE_LOCAL_PATH` | `./data/files` | File storage directory |
| `BENCHLOG_MAX_UPLOAD_SIZE` | `524288000` (500MB) | Max upload size in bytes |

## License

TBD
