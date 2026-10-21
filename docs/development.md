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

The suite creates temporary media, config, and SQLite files and stubs FFmpeg execution. It does not require a physical GPU, NVML, FFmpeg binaries, or a real media library. It exercises command/routing safeguards, output-verification rollback, backup restore/recovery, selected-file behavior, watchdog thresholds/cooldowns, and legacy-container replacement.

Passing these tests does not establish compatibility with a particular NVIDIA driver/GPU, real-world decode support, performance, or perceptual quality. Validate deployment changes with an NVDEC-only selected-file test and then a Full Pipeline Test on a disposable/backed-up file before enabling the bulk queue.

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
app/                  FastAPI application and media/GPU workers
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
- Keep temp, backup, and destination files on the same filesystem/directory for atomic rename behavior.
- Treat SQLite status and filesystem state as a recoverable transaction pair; update startup recovery when changing swap states.
- Keep the bulk queue paused on startup. Selected-file tests remain intentionally independent of bulk queue enablement.
- Update acceptance coverage when changing routing, command construction, recovery, or replacement semantics.
