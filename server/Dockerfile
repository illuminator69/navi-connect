# syntax=docker/dockerfile:1
# The Worbus Server (Worbus P4, design §6.1): the hub layered onto a pinned lb-bot
# image, started by a standard-library launcher under tini.
#
# Build from the umbrella's ROOT, so hub/ and server/ are both in the context:
#   docker build -f server/Dockerfile \
#     --build-arg WORBUS_VERSION=0.6.0 --build-arg WORBUS_COMMIT=$(git rev-parse --short HEAD) .
# BuildKit reads server/Dockerfile.dockerignore beside this file (an allowlist: the
# hub's tests, preview/ and the docs never enter the image).
#
# LBBOT_IMAGE is the lb-bot release this server release holds, by digest, set per
# release (close-out replaces the placeholder below with the pushed digest). A local
# build of both halves passes its own: --build-arg LBBOT_IMAGE=lb-bot:local.
ARG LBBOT_IMAGE=ghcr.io/illuminator69/lb-bot@sha256:2d9f38978b9706b82e16fec640970ae0e543cd523879619d6a547206d36175ed
FROM ${LBBOT_IMAGE}

# lb-bot's image keeps its app in /app (listenbrainz_bot.py, web/dist, fpcalc from
# apt) and sets no USER today. Switch to root for apt and pip in case a later one
# does; the running uid always comes from the compose file's `user:` anyway.
USER root

# tini as PID 1 reaps lb-bot's fpcalc children and forwards SIGTERM to the launcher.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# The hub's one requirement (lb-bot has no package in common with it).
COPY hub/requirements.txt /app/hub/requirements.txt
RUN pip install --no-cache-dir -r /app/hub/requirements.txt
COPY hub/hub.py /app/hub/hub.py
COPY server/worbus_launch.py server/worbus_redact.py server/healthcheck.py /app/server/

ARG WORBUS_VERSION=""
ARG WORBUS_COMMIT=""
ENV WORBUS_VERSION=${WORBUS_VERSION} \
    WORBUS_COMMIT=${WORBUS_COMMIT} \
    WORBUS_DATA=/data \
    WORBUS_HUB_DIR=/app/hub \
    WORBUS_LBBOT_DIR=/app \
    PYTHONUNBUFFERED=1
LABEL org.opencontainers.image.title="Worbus Server" \
      org.opencontainers.image.source="https://github.com/illuminator69/worbus" \
      org.opencontainers.image.version="${WORBUS_VERSION}" \
      org.opencontainers.image.revision="${WORBUS_COMMIT}"

# 4790: the clients (required). 8899: lb-bot's own web page (optional, see compose).
# The hub's health port (4791) stays inside.
EXPOSE 4790 8899
VOLUME /data

# Four misses in a row, 30 s apart, before an lb-bot the launcher is restarting reads
# as unhealthy: 90 to 120 s after it stops answering, depending on where the first
# miss falls between two probes.
HEALTHCHECK --start-period=120s --interval=30s --timeout=5s --retries=4 \
    CMD ["python", "/app/server/healthcheck.py"]

WORKDIR /app
ENTRYPOINT ["tini", "--", "python", "/app/server/worbus_launch.py"]
# lb-bot's image sets CMD ["python", "-u", "listenbrainz_bot.py"]; cleared, or tini
# would hand it to the launcher as arguments.
CMD []
