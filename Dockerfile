FROM node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b AS python-builder

ARG POETRY_VERSION=2.2.1

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/svacer
COPY pyproject.toml poetry.lock ./

# pip only bootstraps the pinned Poetry executable. Poetry installs every
# application dependency from poetry.lock, including exact transitives/hashes.
RUN python3 -m venv /opt/poetry \
    && python3 -m venv /opt/svacer-venv \
    && /opt/poetry/bin/pip install --no-cache-dir "poetry==${POETRY_VERSION}" \
    && VIRTUAL_ENV=/opt/svacer-venv /opt/poetry/bin/poetry sync --without desktop --without dev --no-root \
    && rm -rf /opt/poetry


FROM node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b AS runtime

ARG CODEX_CLI_VERSION=0.155.0-alpha.9.2

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PATH=/opt/svacer-venv/bin:$PATH \
    SVACER_DATA_DIR=/data \
    HOME=/data/home \
    CODEX_HOME=/data/codex-home \
    XDG_CACHE_HOME=/data/cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git openssh-client python3 tini \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@openai/codex@${CODEX_CLI_VERSION}" \
    && npm cache clean --force \
    && codex --version

COPY --from=python-builder /opt/svacer-venv /opt/svacer-venv

WORKDIR /srv/svacer
COPY app ./app
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin svacer \
    && mkdir -p /data/RESULTS /data/home /data/codex-home /data/cache \
    && chown -R svacer:svacer /data /home/svacer

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/srv/svacer/app/container_entrypoint.py"]
