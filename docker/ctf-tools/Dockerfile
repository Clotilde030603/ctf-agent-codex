FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        binutils=2.40-2 \
        binwalk=2.3.4+dfsg1-1 \
        ca-certificates=20250419~deb12u1 \
        checksec=2.6.0-2 \
        file=1:5.44-3 \
        foremost=1.5.7-11+b2 \
        libimage-exiftool-perl=12.57+dfsg-1 \
        tini=0.19.0-1+b3 \
        tshark=4.0.17-0+deb12u3 \
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
