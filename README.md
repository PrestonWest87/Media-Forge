# GPU-Forge

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker Image Version](https://img.shields.io/docker/v/weasts/media-forge?sort=semver)](https://hub.docker.com/r/weasts/media-forge)
[![Docker Pulls](https://img.shields.io/docker/pulls/weasts/media-forge)](https://hub.docker.com/r/weasts/media-forge)

**GPU-Forge** is the media-processing service in the **Media-Forge** repository. It scans mounted libraries, verifies complete video decodes with NVIDIA NVDEC, can intelligently transcode suitable sources to H.265 with `hevc_nvenc`, and integrates with Upscale-Forge plus an independent AAC compatibility queue. A web dashboard provides queue controls, GPU telemetry, progress reporting, settings, and backup recovery.

It is designed for Linux hosts with Docker Compose, a compatible NVIDIA driver, and NVIDIA Container Toolkit. It does not silently fall back to software video decoding or encoding.

## At a glance

- **Purpose:** GPU-accelerated media health checks and optional smart H.265 transcodes.
- **Runtime:** Python 3.12, FastAPI, SQLite, FFmpeg 8.1.2, NVIDIA NVML.
- **Default behavior:** upscale, video, and audio bulk queues paused; operation mode set to health-check only.
- **Docker Hub image:** [`weasts/media-forge`](https://hub.docker.com/r/weasts/media-forge) (`2.1.0` and `latest`).
- **License:** MIT; see [`LICENSE`](LICENSE) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Product screenshots and diagrams

The image slots below are reserved for sanitized product screenshots. Add the corresponding files under [`docs/images/`](docs/images/README.md) and uncomment each Markdown image link when the assets are ready.

<!-- IMAGE SLOT: Add a sanitized screenshot at docs/images/dashboard-overview.png. -->
<!-- ![GPU-Forge dashboard overview](docs/images/dashboard-overview.png) -->

<!-- IMAGE SLOT: Add a sanitized screenshot at docs/images/queue-progress.png. -->
<!-- ![GPU-Forge queue progress and GPU telemetry](docs/images/queue-progress.png) -->

<!-- IMAGE SLOT: Add an architecture illustration at docs/images/architecture-overview.svg. -->
<!-- ![GPU-Forge processing architecture](docs/images/architecture-overview.svg) -->

## Contents

- [Capabilities](#capabilities)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [First-run checklist](#first-run-checklist)
- [How processing works](#how-processing-works)
- [Data, backups, and recovery](#data-backups-and-recovery)
- [Configuration and deployment](#configuration-and-deployment)
- [Documentation map](#documentation-map)
- [Development and tests](#development-and-tests)
- [Security and privacy](#security-and-privacy)
- [Attribution](#attribution)
- [License](#license)

## Capabilities

- Recursively scans configured roots for supported video containers and stores file metadata/job state in SQLite.
- Probes codec, container, bitrate, and resolution during scans, then labels files as **Transcode**, **Leave alone**, or **Upscale candidate**.
- Records verified transcode state and output fingerprints so unchanged outputs stay completed across rescans and queue restarts.
- Identifies low-resolution Upscale-Forge candidates; the sibling worker preserves source frame timestamps and verifies A/V timing before replacement.
- Lets an operator select a candidate, choose 720p/1080p/1440p output, and queue a manual upscale ahead of health/transcode work.
- Provides a separate bulk upscale queue for all eligible low-resolution candidates at the selected target, prioritized ahead of video and audio queues.
- Keeps the original beside every upscaled result until manual restore or purge.
- Runs complete source and output decodes with CUDA surfaces and FFmpeg `-xerror`.
- Routes supported codecs above bitrate efficiency floors to H.265/NVENC when smart transcode mode is enabled.
- Preserves audio streams by copy and handles subtitle mapping according to output container.
- Provides an independently started audio compatibility queue that adds one default AAC-LC stereo fallback while copying the video and preserving original audio tracks.
- Requires output metadata checks and full NVDEC verification before installing a transcode.
- Uses same-directory atomic file renames, optional retained originals, and startup recovery for interrupted swaps.
- Reports core/NVDEC/NVENC utilization, VRAM, temperature, worker progress, queue status, and space savings.
- Pauses its own FFmpeg children to yield to configured external GPU activity and thermal limits.
- Reconciles orphaned yield-paused jobs while preserving already-committed transcodes.
- Exposes a JSON API and interactive OpenAPI documentation at `/docs`.

## Requirements

- Linux host, Docker Engine, and Docker Compose plugin.
- NVIDIA GPU with the required NVDEC decode and NVENC HEVC encode support.
- Compatible NVIDIA driver and NVIDIA Container Toolkit configured for Docker.
- Writable persistent configuration storage and writable media mounts.

GPU support varies by GPU generation, codec profile, pixel format, and driver version. See [deployment and operations](docs/operations.md) for GPU setup and troubleshooting.

## Quick start

### 1. Get the source and prepare host settings

```sh
git clone https://github.com/PrestonWest87/Media-Forge.git
cd Media-Forge
cp .env.example .env
```

Edit `.env` and set `MEDIA_MOVIES_PATH` and `MEDIA_TV_PATH` to existing absolute host paths. The example binds the UI to `127.0.0.1:8090`; change `GPU_FORGE_BIND_IP` only if the service should be reachable on another trusted interface. `.env` is ignored by Git.

Optionally create an explicit runtime configuration file:

```sh
mkdir -p config
cp docs/examples/settings.example.json config/settings.json
```

The application uses built-in settings if the file is absent. Runtime settings and SQLite state are ignored by Git and excluded from the Docker build context.

### 2. Start the service

```sh
docker compose up -d --build
docker compose logs -f gpu-forge
```

Open `http://127.0.0.1:8090` when using the sample bind address. Check `GET /api/health` for API and NVML status. The API's interactive documentation is at `/docs` and ReDoc at `/redoc`.

### 3. Start safely

The first startup scan indexes the configured roots, but all bulk queues remain paused. Use **Scan Library**, select one file, and run **NVDEC-only Test** first. For a supported non-HEVC source, **Full Pipeline Test** exercises the complete encode/verification path and retains the original backup. Choose **Check + Smart H.265 Transcode** and click **Start Video Queue** only when bulk video work is intended. Choose an upscale target and click **Start Upscale Queue** to enqueue eligible low-resolution candidates, or click **Start Audio Queue** separately to add AAC fallback tracks without re-encoding video.

## First-run checklist

1. Confirm `docker compose logs gpu-forge` shows the app starting without configuration or mount errors.
2. Confirm `/api/health` reports `ok: true` and inspect `gpu_available` / `gpu_error`.
3. Verify the dashboard lists media files and the configured GPU index.
4. Run one NVDEC-only test and inspect its result before processing a library.
5. For transcode validation, choose a non-critical file and run Full Pipeline Test; confirm its retained backup before restoring or purging it.
6. Review `backup_policy` and operation mode before enabling the bulk queue.

## How processing works

### Health-check mode

Each supported source is fully decoded by NVDEC. GPU-Forge records metadata and result status without changing the source bytes.

### Smart H.265 mode

GPU-Forge runs a source NVDEC check, then considers H.264, MPEG-2 Video, MPEG-4 Part 2, VC-1, VP8, and VP9 sources whose measured bitrate exceeds a resolution-dependent floor. HEVC/AV1 and ineligible codecs receive health checks rather than a lossy second encode. H.264 10-bit is explicitly rejected for this hardware decode path.

The transcode uses `hevc_nvenc`. The temporary output must be non-empty, preserve required duration/audio metadata, save at least 7%, and pass a full NVDEC decode. Only after these gates pass does a same-directory rename install it. A retained backup policy keeps the original available after successful replacement.

### Audio compatibility queue

The separate **Audio Queue** selects the first default audio stream (or the first audio stream when no default is marked). If it is not already AAC-LC mono/stereo at 48 kHz or lower, GPU-Forge copies the video and all original audio streams, adds one AAC-LC stereo fallback, and marks the new track as the default. The audio queue runs below the video queue priority, does not run the H.265 encoder, and does not call Jellyfin; refresh the Jellyfin library separately after processing.

### Bulk upscale queue

The separate **Upscale Queue** enqueues every eligible `UPSCALE` candidate at the target selected in the dashboard. It remains paused until started and runs ahead of video and audio bulk work. Individual **Upscale Selected** jobs remain available and take priority over the bulk upscale queue.

### GPU scheduling

NVML telemetry is polled about once per second. When external GPU activity exceeds the configured threshold, or the card reaches the thermal pause point, GPU-Forge pauses only its own FFmpeg child processes. It resumes after the configured cooling/idle conditions are met. See [architecture](docs/architecture.md) for exact behavior.

## Data, backups, and recovery

- `./config:/config` persists `settings.json` and the SQLite database.
- `.gpuforge_tmp*` files are temporary outputs and are removed by safe startup recovery when abandoned.
- `.gpuforge_bak` files hold original media during replacement or while a retained backup is available.
- `.gpuforge_audio_bak` files are temporary rollback copies used when an audio remux operates on media that already has a retained backup.
- The default `auto_delete` policy removes a backup after a successful verified replacement. `keep` retains it for a manual restore or purge.
- Full Pipeline Test forces backup retention when it performs a replacement.
- The dashboard's **Restore Selected Backup** restores a retained original; **Purge Verified Backups** permanently deletes eligible retained backups.

Backups reside next to media, not under `config/`. For operational details and recovery states, see [deployment and operations](docs/operations.md).

## Configuration and deployment

The Compose template reads local paths and network binding from `.env`; `config/settings.json` controls runtime policy. Defaults and validation rules are in [Configuration](docs/configuration.md). Read [Deployment and operations](docs/operations.md) before exposing the service to a network.

## Documentation map

| Document | Coverage |
| --- | --- |
| [Operations](docs/operations.md) | NVIDIA/Docker setup, first-run workflow, backups, maintenance, and troubleshooting. |
| [Configuration](docs/configuration.md) | Complete settings table, Compose variables, paths, and route thresholds. |
| [API reference](docs/api-reference.md) | Endpoints, query parameters, response fields, filters, and errors. |
| [Architecture](docs/architecture.md) | Service lifecycle, scanner, watchdog, processing gates, and recovery. |
| [Development](docs/development.md) | Local Python setup, tests, source layout, and implementation invariants. |
| [Docker Hub publishing](docs/dockerhub-publish.md) | Image identity, metadata, SBOM-enabled build, and release push. |
| [Docker Hub description](docs/dockerhub-description.md) | Copy-ready branded repository summary and deployment notes. |
| [Third-party notices](THIRD_PARTY_NOTICES.md) | Runtime packages, image bases, frontend assets, and license references. |
| [Security policy](SECURITY.md) | Deployment exposure and vulnerability reporting. |
| [Contributing](CONTRIBUTING.md) | Change and review expectations. |
| [Visual asset slots](docs/images/README.md) | Reserved screenshot/diagram assets and sanitization guidance. |

## Development and tests

Use Python 3.12 and install dependencies in an isolated environment:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The CPU-only acceptance suite stubs FFmpeg and needs no GPU or real media library. It covers command safeguards, scan metadata/action tagging, transcode persistence, yield recovery, routing, verification rollback, startup recovery, selected tests, watchdog logic, and legacy-container replacement. It does not verify real driver compatibility or perceived video quality.

## Security and privacy

The HTTP API currently has no built-in authentication. Bind it to loopback or a trusted network and use an authenticated reverse proxy for remote access. Do not commit `.env`, `config/settings.json`, database files, media, logs with sensitive paths, registry credentials, or access tokens. The repository's `.gitignore` and `.dockerignore` exclude local runtime state; review the exact file list and run a secret scan before publishing changes.

The browser dashboard currently loads Tailwind CSS from its CDN. The server does not require a third-party account for normal operation. See [`SECURITY.md`](SECURITY.md).

## Attribution

The project plan is attributed to Google Gemini, Argon 4 High. Implementation is attributed to the OpenCode coding agent powered by `opencode/gpt-6-luna`; the project owner's requested display attribution is “gpt-6 Luna Max.” See [`CREDITS.md`](CREDITS.md) for the complete acknowledgement and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for package and asset credits. These acknowledgements do not imply endorsement by Google, OpenCode, NVIDIA, Docker, or other upstream projects.

## License

GPU-Forge is distributed under the MIT License; see [`LICENSE`](LICENSE). Third-party packages and base images remain under their respective licenses, listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
