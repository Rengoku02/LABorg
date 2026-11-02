# syntax=docker/dockerfile:1.7
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .

ENV PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=labstock.settings.prod

RUN DJANGO_SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput \
 && useradd --system --uid 10001 --home /app labstock \
 && mkdir -p /app/media \
 && chown -R labstock /app/media

USER labstock
EXPOSE 8000

# No Gunicorn access log: request paths can carry signed action tokens. Caddy logs access
# with those tokens redacted (see Caddyfile).
CMD ["gunicorn", "labstock.wsgi", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
