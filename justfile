set shell := ["bash", "-cu"]

default:
    @just --list

dev:
    docker compose up db -d
    uv run alembic upgrade head
    trap 'kill 0' EXIT; \
    ./tailwindcss -i benchlog/static/css/input.css -o benchlog/static/css/output.css --watch & \
    uv run uvicorn benchlog.main:app --reload & \
    wait
