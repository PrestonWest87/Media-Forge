FROM python:3.12-slim-bookworm AS python312

FROM linuxserver/ffmpeg:version-8.1.2-cli

ARG APP_VERSION=2.0.0
ARG VCS_REF=unknown

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

RUN apt-get update && apt-get install -y --no-install-recommends \
    libbz2-1.0 \
    libexpat1 \
    libffi8 \
    libgdbm6 \
    liblzma5 \
    libncursesw6 \
    libreadline8 \
    libsqlite3-0 \
    libssl3t64 \
    libuuid1 \
    procps \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
LABEL org.opencontainers.image.title="GPU-Forge" \
      org.opencontainers.image.description="GPU-accelerated media verification and smart H.265 transcoding with NVIDIA NVDEC/NVENC" \
      org.opencontainers.image.source="https://github.com/PrestonWest87/Media-Forge" \
      org.opencontainers.image.documentation="https://github.com/PrestonWest87/Media-Forge/tree/main/docs" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="weasts" \
      org.opencontainers.image.authors="PrestonWest87 (weasts)" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
COPY --from=python312 /usr/local/ /usr/local/
COPY requirements.txt .
RUN python3.12 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/
ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8090
ENTRYPOINT []
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
