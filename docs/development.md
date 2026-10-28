# Development and tests

## Local Python setup

Use Python 3.12 to match the service image:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run the CPU-only acceptance suite from the repository root:

```sh
python -m unittest discover -s tests -v
```

The suite creates temporary media, config, and SQLite files and stubs FFmpeg execution. It does not require a physical GPU, NVML, FFmpeg binaries, or a real media library. It exercises command/routing safeguards, scan-time metadata/action tags, persistent video/audio/upscale work tracking, audio stream selection and default disposition, separate bulk queue start/pause and priority, output-verification rollback, backup restore/recovery, yield-state recovery, selected-file behavior, watchdog thresholds/cooldowns, and legacy-container replacement.

Passing these tests does not establish compatibility with a particular NVIDIA driver/GPU, real-world decode support, performance, or perceptual quality. Validate video changes with an NVDEC-only selected-file test and a Full Pipeline Test. Validate audio changes with a real FFmpeg remux of a disposable file, then inspect the streams/default disposition with FFprobe and play the file in Jellyfin after a manual library refresh.

## Run the API locally

From the repository root, after installing dependencies:

```sh
GPU_FORGE_CONFIG_DIR="$PWD/config" \
GPU_FORGE_DB_PATH="$PWD/config/gpu_forge.db" \
uvicorn app.main:app --host 127.0.0.1 --port 8090
```

The local service requires the same runtime tools as the container. Media processing requires `ffprobe`, a CUDA/NVDEC/NVENC-capable FFmpeg build, accessible NVIDIA driver libraries/NVML, and a visible supported GPU. The API can start without NVML, but hardware media work will be skipped as unsupported.

Use temporary paths for `GPU_FORGE_CONFIG_DIR` and `GPU_FORGE_DB_PATH` when developing so local experiments do not mix with a live service's `/config` state.

## Source layout

```text
app/                  FastAPI application, scanner, FFprobe helpers, and media/GPU workers
app/static/index.html Browser dashboard
config/               Local runtime state (ignored); examples live in docs/examples/
docs/                 User, API, architecture, operations, and development docs
tests/                CPU-only acceptance suite
Dockerfile            FFmpeg/Python service image and OCI metadata
docker-compose.yml    GPU-enabled deployment template
requirements.txt      Python runtime dependency ranges
```

## Implementation invariants

- All media video decodes must use CUDA output surfaces and `-xerror`; do not add silent software fallback.
- Never replace a source until output metadata and full NVDEC verification pass.
- Audio compatibility jobs copy video, preserve every original audio track, and add at most one verified default AAC-LC stereo fallback for the primary audio stream.
- Keep the user-triggered bulk-upscale, video/health, and audio queues independent; dispatch individual and bulk upscales first, video jobs before audio jobs, and serialize all FFmpeg work through the single worker.
- Keep temp, backup, and destination files on the same filesystem/directory for atomic rename behavior.
- Treat SQLite status and filesystem state as a recoverable transaction pair; update startup recovery when changing swap states.
- Persist output fingerprints and a transcode timestamp; an unchanged verified output must not return to the queue after a rescan.
- Persist `PAUSED_YIELD` only while a live FFmpeg child is stopped, and reconcile orphaned active states without overwriting committed transcodes.
- Keep all bulk queues paused on startup. Selected-file tests and manual upscales remain intentionally independent of bulk queue enablement.
- Update acceptance coverage when changing routing, command construction, recovery, or replacement semantics.
