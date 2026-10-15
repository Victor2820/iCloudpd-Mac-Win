# syntax=docker/dockerfile:1.7
#
# Multi-arch image for the customized icloudpd.
#
# Like the rest of this repository, the build does not carry upstream source: it
# fetches the pinned upstream tarball, applies custom/ on top, and installs the
# result with pip. Installing the package -- rather than copying a PyInstaller
# binary in -- is what makes this image buildable for amd64 and arm64 without a
# cross toolchain: everything upstream depends on ships a wheel.
#
# Build from the root of this repository (the context only needs custom/):
#     docker build -f custom/Dockerfile -t icloudpd .
# Multi-arch:
#     docker buildx build -f custom/Dockerfile --platform linux/amd64,linux/arm64 .

ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS builder

ARG UPSTREAM_REPO=icloud-photos-downloader/icloud_photos_downloader
ARG UPSTREAM_SHA=879c561240d993d748ddb4546f935090502b16d3
ARG ICLOUDPD_VERSION=1.32.3
ARG ICLOUDPD_COMMIT_SHA=879c561
ARG ICLOUDPD_COMMIT_TS=1780177342

ENV ICLOUDPD_VERSION=$ICLOUDPD_VERSION \
    ICLOUDPD_COMMIT_SHA=$ICLOUDPD_COMMIT_SHA \
    ICLOUDPD_COMMIT_TS=$ICLOUDPD_COMMIT_TS

RUN apt-get update \
 && apt-get install --no-install-recommends -y ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN curl -fsSL -o up.tar.gz \
      "https://codeload.github.com/${UPSTREAM_REPO}/tar.gz/${UPSTREAM_SHA}" \
 && tar xzf up.tar.gz --strip-components=1 \
 && rm up.tar.gz

COPY custom/ /custom/
RUN chmod +x /custom/apply_customizations.sh /custom/docker-entrypoint.sh \
 && /custom/apply_customizations.sh

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --disable-pip-version-check --no-cache-dir --upgrade "pip>=25.1" \
 && /opt/venv/bin/pip install --disable-pip-version-check --no-cache-dir .

FROM python:${PYTHON_VERSION}-slim AS runtime

# tzdata, not just TZ: the tool converts asset dates to the local zone when it
# builds folder names, and a slim image has no zoneinfo at all.
RUN apt-get update \
 && apt-get install --no-install-recommends -y tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY custom/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH="/opt/venv/bin:$PATH" \
    TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

LABEL org.opencontainers.image.title="icloudpd" \
      org.opencontainers.image.description="iCloud Photos Downloader with a guided setup, a polling wait for unfinished iCloud indexing and opt-in --ignore-indexing-state" \
      org.opencontainers.image.source="https://github.com/Victor2820/iCloudpd-Mac-Win" \
      org.opencontainers.image.licenses="MIT"

# the folder people mount their photos on
VOLUME ["/data"]
WORKDIR /data
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]
