FROM python:3.12-slim-bookworm AS ffmpeg-build

ARG FFMPEG_TAG=n8.1.2
ARG NVCODEC_TAG=n13.0.19.1

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    git \
    nasm \
    pkg-config \
    yasm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone --depth 1 --branch "${FFMPEG_TAG}" https://github.com/FFmpeg/FFmpeg.git ffmpeg && \
    git clone --depth 1 --branch "${NVCODEC_TAG}" https://github.com/FFmpeg/nv-codec-headers.git nv-codec-headers && \
    make -C nv-codec-headers install PREFIX=/usr/local

WORKDIR /build/ffmpeg
RUN ./configure \
      --prefix=/usr/local \
      --disable-debug \
      --disable-doc \
      --disable-ffplay \
      --disable-static \
      --enable-shared \
      --enable-cuvid \
      --enable-nvdec \
      --enable-nvenc && \
    make -j"$(nproc)" && \
    make install && \
    ldconfig && \
    /usr/local/bin/ffmpeg -hide_banner -encoders 2>&1 | grep -q 'hevc_nvenc' && \
    /usr/local/bin/ffmpeg -hide_banner -encoders 2>&1 | grep -q ' aac ' && \
    /usr/local/bin/ffmpeg -hide_banner -hwaccels 2>&1 | grep -q 'cuda' && \
    ! /usr/local/bin/ffmpeg -hide_banner -buildconf 2>&1 | grep -Eq -- '--enable-(gpl|nonfree)' && \
    mkdir -p /usr/share/doc/gpu-forge/ffmpeg/sources && \
    git -C /build/ffmpeg archive --format=tar.gz --prefix="ffmpeg-${FFMPEG_TAG}/" HEAD \
      > /usr/share/doc/gpu-forge/ffmpeg/sources/ffmpeg-${FFMPEG_TAG}-source.tar.gz && \
    git -C /build/nv-codec-headers archive --format=tar.gz --prefix="nv-codec-headers-${NVCODEC_TAG}/" HEAD \
      > /usr/share/doc/gpu-forge/ffmpeg/sources/nv-codec-headers-${NVCODEC_TAG}-source.tar.gz && \
    { \
      printf 'FFmpeg tag: %s\n' "${FFMPEG_TAG}"; \
      printf 'FFmpeg commit: %s\n' "$(git -C /build/ffmpeg rev-parse HEAD)"; \
      printf 'NV codec headers tag: %s\n' "${NVCODEC_TAG}"; \
      printf 'NV codec headers commit: %s\n' "$(git -C /build/nv-codec-headers rev-parse HEAD)"; \
      printf '%s\n' 'Configure: --disable-debug --disable-doc --disable-ffplay --disable-static --enable-shared --enable-cuvid --enable-nvdec --enable-nvenc'; \
      printf '%s\n' 'License profile: LGPL-2.1-or-later; --enable-gpl and --enable-nonfree are not used.'; \
      printf '%s\n' 'The corresponding FFmpeg and nv-codec-headers source archives are in this directory.'; \
    } > /usr/share/doc/gpu-forge/ffmpeg/BUILD-INFO.txt

RUN /usr/local/bin/ffmpeg -hide_banner -L 2>&1 | grep -q 'GNU Lesser General Public'

FROM python:3.12-slim-bookworm AS runtime

ARG APP_VERSION=2.1.0
ARG VCS_REF=unknown

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libbz2-1.0 \
    libexpat1 \
    libffi8 \
    libgdbm6 \
    liblzma5 \
    libncursesw6 \
    libreadline8 \
    libsqlite3-0 \
    libssl3 \
    libuuid1 \
    procps \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg-build /usr/local/ /usr/local/
COPY --from=ffmpeg-build /usr/share/doc/gpu-forge/ffmpeg/ /usr/share/doc/gpu-forge/ffmpeg/
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/gpu-forge/
RUN ldconfig

WORKDIR /app
LABEL org.opencontainers.image.title="GPU-Forge" \
      org.opencontainers.image.description="GPU-accelerated media verification, H.265 transcoding, bulk upscaling integration, and default AAC compatibility tracks" \
      org.opencontainers.image.source="https://github.com/PrestonWest87/Media-Forge" \
      org.opencontainers.image.documentation="https://github.com/PrestonWest87/Media-Forge/tree/main/docs" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="weasts" \
      org.opencontainers.image.authors="PrestonWest87 (weasts)" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
COPY requirements.txt .
RUN python3.12 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/
ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8090
ENTRYPOINT []
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
