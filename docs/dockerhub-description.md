# GPU-Forge — NVIDIA media verification and H.265 transcoding

GPU-Forge scans mounted media libraries, verifies video files with NVIDIA NVDEC, and can smart-route eligible sources to H.265 encoding with NVENC. The dashboard provides queue controls, GPU telemetry, live FFmpeg progress, and retained-backup restore/purge actions.

**Image target:** [`weasts/media-forge`](https://hub.docker.com/r/weasts/media-forge) (no tag published yet)<br>
**Source:** [PrestonWest87/Media-Forge](https://github.com/PrestonWest87/Media-Forge)<br>
**Platform:** Linux `amd64`<br>
**License:** MIT for GPU-Forge source; image components retain their respective licenses (see [third-party notices](https://github.com/PrestonWest87/Media-Forge/blob/main/THIRD_PARTY_NOTICES.md)).

## Requirements

- Linux host with Docker Compose.
- NVIDIA GPU with supported NVDEC/NVENC features.
- Compatible NVIDIA driver and NVIDIA Container Toolkit.
- Writable host media paths and persistent configuration storage.

## Quick start

This repository description is a copy-ready template for use after a redistribution-compatible image release is published. The current Dockerfile base has not been published to Docker Hub.

Clone the source and use its Compose template:

```sh
git clone https://github.com/PrestonWest87/Media-Forge.git
cd Media-Forge
cp .env.example .env
```

Edit `.env` with absolute, existing host paths for `MEDIA_MOVIES_PATH` and `MEDIA_TV_PATH`; the sample binds the dashboard to loopback on port 8090. Then set `GPU_FORGE_IMAGE=weasts/media-forge:latest` in `.env` and start:

```sh
docker compose pull
docker compose up -d
docker compose logs -f gpu-forge
```

Open `http://127.0.0.1:8090` for the sample bind. The bulk queue starts paused in health-check-only mode. Run a selected-file NVDEC-only test before enabling bulk transcodes.

Media mounts must be writable because verified replacements use same-directory temporary files and atomic renames. GPU-Forge does not use software video decode/encode fallback. The dashboard/API has no built-in authentication; restrict network access or use an authenticated reverse proxy.

## Documentation

- [Full README and quick start](https://github.com/PrestonWest87/Media-Forge#readme)
- [Deployment and operations](https://github.com/PrestonWest87/Media-Forge/blob/main/docs/operations.md)
- [Configuration reference](https://github.com/PrestonWest87/Media-Forge/blob/main/docs/configuration.md)
- [API reference](https://github.com/PrestonWest87/Media-Forge/blob/main/docs/api-reference.md)
- [Security policy](https://github.com/PrestonWest87/Media-Forge/blob/main/SECURITY.md)
