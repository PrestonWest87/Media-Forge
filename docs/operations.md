# Deployment and operations

## Host prerequisites

1. Install Docker Engine and the Docker Compose plugin on a Linux host.
2. Install a compatible NVIDIA driver and NVIDIA Container Toolkit, then configure Docker to use the NVIDIA runtime/toolkit.
3. Confirm the GPU is working on the host and visible in a test container before starting GPU-Forge.
4. Choose persistent configuration storage and writable media directories. The container must be able to read and write every configured media root.

The image uses Python 3.12 on Debian Bookworm and builds FFmpeg 8.1.2 with `nv-codec-headers` 13.0.19.1. NVDEC and `hevc_nvenc` are enabled without GPL or nonfree FFmpeg flags; the matching FFmpeg/header source archives and build configuration are included under `/usr/share/doc/gpu-forge/ffmpeg/`. The NVIDIA driver is supplied by the host's NVIDIA Container Toolkit. Supported decoding/encoding depends on GPU generation, codec profile/pixel format, and driver compatibility.

## Configure and launch

```sh
git clone https://github.com/PrestonWest87/Media-Forge.git
cd Media-Forge
cp .env.example .env
```

Edit `.env` before launch:

- Set `MEDIA_MOVIES_PATH` and `MEDIA_TV_PATH` to existing absolute host directories.
- Keep the Compose container paths `/media/movies` and `/media/tv` aligned with `media_roots` in `config/settings.json` (or use the built-in defaults).
- The sample binds to `127.0.0.1:8090`. Use another trusted host IP for LAN access; use `0.0.0.0` only when all network clients should be able to reach it.
- Keep media mounts writable. Transcodes are written beside sources and installed using atomic renames.
- Keep `GPU_FORGE_CONFIG_PATH` on persistent storage so settings and SQLite state survive container replacement.

Optional explicit settings and service startup:

```sh
mkdir -p config
cp docs/examples/settings.example.json config/settings.json
docker compose up -d --build
docker compose logs -f gpu-forge
```

Open the UI at `http://<GPU_FORGE_BIND_IP>:<GPU_FORGE_PORT>`, for example `http://127.0.0.1:8090`. `GET /api/health` reports API and NVML status. `ok: true` means the API is answering; it does not guarantee GPU access (`gpu_available` must also be true).

## First-run workflow

1. Wait for the automatic startup scan or click **Scan Library**.
2. Confirm GPU telemetry is available and inspect the visible GPU index.
3. Select one file and run **NVDEC-only Test**. This checks hardware decoding without changing the file.
4. For an applicable source, run **Full Pipeline Test** to validate source decode, H.265 encode, output checks, and backup retention.
5. Select **Check + Smart H.265 Transcode** only when encoding is intended. Built-in settings default to **Health Check Only**.
6. Click **Start Full Queue** when ready to process pending items. The bulk queue starts paused intentionally.

Selected-file tests can run while the bulk queue is paused. A queue pause disables new bulk work but lets the current file finish safely. Running a scan with the queue enabled enqueues new pending items; while paused it only indexes them.

## Dashboard and queue behavior

The dashboard shows core/NVDEC/NVENC utilization, VRAM, temperature, queue state, overall progress, current FFmpeg phase/progress, saved bytes, retained-backup size, file statuses, and exact processing errors. Available views are active/pending, corrupt, transcoded, skipped, unsupported, and failed.

The service uses one worker. Selected-file tests are prioritized over normal queued files. Processing phases are `CHECKING`, `TRANSCODING`, and `VERIFYING_OUTPUT`. A pause caused by GPU use or heat is reflected as `PAUSED_YIELD`; telemetry differentiates thermal pausing.

Common statuses:

| Status | Meaning |
| --- | --- |
| `PENDING` | Indexed and waiting for work. |
| `HEALTHY` | Full source NVDEC check passed without a transcode. |
| `TRANSCODED` | H.265 output passed verification and the original backup was removed. |
| `TRANSCODED_BACKUP_KEPT` | Verified replacement installed and original retained. |
| `SKIPPED_OPTIMAL` | An attempted transcode saved less than 7%; the original was left untouched. Sources below smart-route bitrate floors receive a health check and are reported as `HEALTHY`. |
| `SKIPPED_HW_UNSUPPORTED` | Required codec, bit depth, GPU, or driver hardware path is unsupported. No software video fallback is used. |
| `CORRUPT` | Source metadata or decode indicates an unreadable/damaged input. |
| `TRANSCODE_FAILED_RESTORED` | Transcode failed and original stayed in place or was restored. |
| `PAUSED_YIELD` | Worker is paused for external GPU activity or thermal control. |

## Backup, restore, and purge

Every candidate output is first written as a temporary sibling file. During a swap the original is renamed to `<original-path>.gpuforge_bak`; the verified output is then installed. Backup behavior after success:

- `auto_delete` removes the original backup after the verified replacement is committed.
- `keep` retains the original and marks the row `TRANSCODED_BACKUP_KEPT`.
- If automatic cleanup fails after the replacement is committed, the backup is retained and exposed rather than silently discarded.
- Full Pipeline Test forces backup retention if it transcodes a file.

Use **Restore Selected Backup** to replace a verified output with its retained original. Legacy formats are restored to their original filename/extension. Use **Purge Verified Backups** only after confirming the originals are no longer needed; purge is permanent. The UI asks for confirmation before either destructive operation.

Backups live beside media and are excluded from ordinary scans. On startup, recovery removes abandoned temporary outputs, restores originals for incomplete transactions, cleans committed auto-delete backups left by a crash, reconciles stale database backup references, and requeues safe interrupted work. Avoid manually deleting `.gpuforge_bak` artifacts until recovery has completed.

## GPU scheduling and temperature

NVML is polled approximately once per second. For external-work yielding, an external PID must be detected and the busiest core/decode/encode engine must exceed the configured threshold. `known` recognizes external FFmpeg/Jellyfin processes; `any` considers any PID returned by NVML. When a new external PID appears during GPU-Forge work, the watchdog briefly pauses its own FFmpeg child to sample external utilization independently.

Only GPU-Forge's registered FFmpeg children are signaled. External processes are never stopped by this service. After confirmed external GPU activity, resumption requires a 15-second low-utilization cooldown. A newly observed PID is first probed while the internal worker is paused; if that probe shows no meaningful external load, work can resume immediately. Thermal protection pauses at the lower of the configured limit and 5 °C below NVML's slowdown threshold, then resumes at least 7 °C cooler after the configured dwell period.

## Logs and maintenance

```sh
docker compose ps
docker compose logs --tail=200 gpu-forge
docker compose logs -f gpu-forge
```

The default log level is `INFO`; set `LOG_LEVEL=DEBUG` in the service environment for diagnostic detail. The app uses a single Uvicorn worker because the in-process queue and watchdog are not distributed across processes.

Update the container after updating the source:

```sh
docker compose up -d --build
```

Back up the mounted config directory before major changes if the index/settings must be preserved. Retained media backups are separate and remain beside the media files.

## Troubleshooting

| Symptom | Checks |
| --- | --- |
| `gpu_available` is false | Check host driver and NVIDIA Container Toolkit, Compose GPU reservation, `compute,video,utility` capabilities, GPU visibility/index, and container logs. |
| Scan finds no files | Check `.env` host paths, container mount locations, `media_roots`, permissions, supported extensions, and that media is not only reachable through symlinks (symlinks are not followed). |
| `SKIPPED_HW_UNSUPPORTED` | Check GPU/driver codec support, FFmpeg NVDEC/NVENC libraries, NVML access, and bit depth. H.264 10-bit is explicitly skipped. |
| `SKIPPED_OPTIMAL` after encode | Output saved less than 7%; the original was not replaced. Low-bitrate inputs can also be routed to health check only. |
| Permission/ownership error | Ensure the container can write inside each source directory and preserve source owner/mode. Temp, backup, and destination files must remain on a writable filesystem. |
| Queue is paused | Click **Start Full Queue** after scanning. Queue pause finishes current work and prevents additional bulk jobs. Selected-file tests remain usable. |
| GPU work repeatedly pauses | Review GPU utilization/temperature and external/thermal thresholds. `known` process detection depends on NVML and Linux `/proc` visibility. |
| A backup remains after success | This is expected for `keep` or Full Pipeline Test. Otherwise inspect the row error; automatic backup cleanup can fail and preserve the original. |

## Network exposure

The UI/API currently has no built-in authentication. Prefer loopback plus an authenticated reverse proxy, or restrict the host port to a trusted network. The Uvicorn command trusts forwarded headers from all peers, so direct access to the container port should be limited to trusted clients/proxies. See [`SECURITY.md`](../SECURITY.md).
