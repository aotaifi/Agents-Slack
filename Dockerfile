# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
COPY requirements.lock ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -r requirements.lock
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts/start.sh ./scripts/start.sh
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app \
    && chmod -R a+rX /app/src /app/alembic /app/scripts \
    && chmod a+r /app/alembic.ini \
    && mkdir -p /app/.local && chmod 700 /app/.local && chown -R app:app /app/.local
USER app
EXPOSE 8000
CMD ["sh", "scripts/start.sh"]
