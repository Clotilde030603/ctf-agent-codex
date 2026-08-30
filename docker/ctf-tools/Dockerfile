FROM python:3.12.11-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        binutils \
        binwalk \
        ca-certificates \
        checksec \
        file \
        foremost \
        libimage-exiftool-perl \
        tini \
        tshark \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 ctf \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin ctf

LABEL org.opencontainers.image.title="ctf-agent-codex tools" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.description="Non-root offline tool image for authorized CTF workers"

WORKDIR /work
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "--version"]
