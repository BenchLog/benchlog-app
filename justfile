set shell := ["bash", "-cu"]

default:
    @just --list

dev:
    docker compose up db -d
    uv run alembic upgrade head
    trap 'kill 0' EXIT; \
    just css-watch & \
    uv run uvicorn benchlog.main:app --reload

css-build:
    ./tailwindcss -i benchlog/static/css/input.css -o benchlog/static/css/output.css

css-watch:
    ./tailwindcss -i benchlog/static/css/input.css -o benchlog/static/css/output.css --watch

test:
    docker compose up db -d
    uv run pytest

lint:
    uv run ruff check .
