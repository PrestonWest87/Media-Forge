# Architecture

GPU-Forge is a single-process FastAPI service with one asynchronous media worker. Its lifespan initializes persistent state and startup recovery before enabling the scanner, NVML watchdog, and worker.

## Components

| Component | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI routes, service lifecycle, ordered startup recovery. |
| `app/config.py` | Pydantic settings model and atomic JSON settings persistence. |
| `app/db.py` | SQLite schema, media metadata/status queries, saved-space calculations. |
| `app/scanner.py` | Non-following directory traversal and batched video indexing. |
| `app/worker.py` | FFprobe metadata, GPU FFmpeg command construction, codec routing, verification, and transactional replacement. |
| `app/gpu_watchdog.py` | NVML telemetry, external-process detection, thermal policy, internal FFmpeg pause/resume. |
| `app/recovery.py` | Abandoned temporary cleanup and repair/requeue of interrupted swaps/jobs. |
| `app/static/index.html` | Single-page dashboard and browser-side API client. |

## Startup sequence

1. Load settings, initialize SQLite/WAL, and create schema if absent.
2. Run recovery before new worker work. Recovery reconciles `.gpuforge_tmp` and `.gpuforge_bak` files with persisted row state.
3. Start the NVML watchdog and media worker. The bulk queue remains disabled.
4. Launch an asynchronous initial scan of configured `media_roots`.

The startup scan indexes files but does not enable the bulk queue. Selected-file tests can run while the bulk queue is paused.

## Scanning and persistence

The scanner recursively uses `os.scandir`, does not follow file or directory symlinks, and considers these extensions case-insensitively:

`.3gp`, `.asf`, `.avi`, `.flv`, `.m2ts`, `.m2v`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.mpeg`, `.mpg`, `.mts`, `.mxf`, `.ogv`, `.ts`, `.vob`, `.webm`, `.wmv`.

Names containing `.gpuforge_tmp` or `.gpuforge_bak` are excluded from normal scans. File path, size, and modification time detect changes; changed source fingerprints reset prior processing metadata/status to `PENDING`.

SQLite stores one row per tracked path, including codec, pixel format, bitrate, dimensions, duration, audio/subtitle counts, backup path, output size, status, errors, and processing time. The default database path is `/config/gpu_forge.db`.

## Processing and routing

The worker checks that the file exists and asks `ffprobe` for stream metadata before media decoding. It rejects missing/empty sources, unreadable containers, missing video streams, and unsupported codec/bit-depth paths. Every media FFmpeg decode command must use:

- `-xerror` to fail when decoding reports an error.
- `-hwaccel cuda` and `-hwaccel_output_format cuda` to keep video decode on CUDA surfaces.
- The configured visible GPU index.

The service does not use software video decode/encode fallback. If GPU/NVML is unavailable or a source is outside the NVDEC whitelist, the job is marked unsupported without a software video decode.

In `health_check` mode, the worker performs a full NVDEC decode and does not transcode. Smart transcode mode requires one of `{h264, mpeg2video, mpeg4, vc1, vp8, vp9}` and a known bitrate strictly above the resolution floor: 0.6 Mbit/s up to 480p, 1.1 Mbit/s up to 720p, and 2.2 Mbit/s above 720p/unknown. HEVC/AV1 are health-check only. H.264 10-bit is explicitly unsupported by this decode path.

Transcodes use `hevc_nvenc`, selected CQ profile/preset, and audio stream copy. MP4/M4V output uses an MP4-compatible HEVC tag; other output uses Matroska. Legacy/non-target containers are installed with an `.mkv` filename if a safe destination is available. MP4/M4V subtitle mapping accepts `mov_text`; Matroska output maps subtitle and data streams for copy.

## Verification and atomic replacement

FFmpeg success alone is not enough to replace a source. The worker:

1. Requires a non-empty output and verifies source size/mtime did not change while processing.
2. For normal queue work, discards output that saves less than 7%.
3. Probes output and requires HEVC, duration within 0.5 seconds, identical audio stream count, and for MKV inputs identical subtitle count.
4. Runs a full output NVDEC decode with `-xerror`.
5. Copies source ownership/mode to the output, syncs it, and performs same-directory atomic renames.
6. Commits the media row only after output installation; then deletes or retains the original backup per policy.

Temporary outputs are named `<source-path>.gpuforge_tmp<extension>`. Backups use `<original-path>.gpuforge_bak`. When output container changes, the old-extension backup remains beside the new `.mkv` destination. Existing backups and conflicting destinations are not overwritten.

Startup recovery prefers the original when a swap was not committed, keeps a retained backup for committed `TRANSCODED_BACKUP_KEPT` rows, removes safe leftover temporary/auto-clean artifacts, reconciles stale backup references, and requeues interrupted jobs where possible.

## GPU watchdog

NVML is polled about once per second for core, NVDEC, NVENC, memory, temperature, and process telemetry. External yielding requires an external PID and the busiest GPU engine above the configured threshold. `known` filters to process command/name values containing FFmpeg/Jellyfin; `any` accepts all external PIDs exposed by NVML.

The watchdog signals only registered GPU-Forge FFmpeg children. Thermal pause uses the lower of the configured setting and 5 °C below the NVML slowdown point (with a 60 °C floor); resumption requires cooling 7 °C below that point and a configured dwell. Confirmed external-work resume uses a 15-second low-utilization cooldown. A newly observed external PID is sampled with the worker paused; if that fresh sample is idle, the worker may resume immediately.

## Typical status lifecycle

```text
PENDING -> CHECKING -> HEALTHY
                     -> SKIPPED_HW_UNSUPPORTED
                     -> CORRUPT
                     -> TRANSCODING -> VERIFYING_OUTPUT -> TRANSCODED
                                                        -> TRANSCODED_BACKUP_KEPT
                                                        -> TRANSCODE_FAILED_RESTORED
                                                        -> SKIPPED_OPTIMAL
```

`PAUSED_YIELD` is persisted while the watchdog has paused FFmpeg; telemetry distinguishes thermal pause state. Failures before replacement leave the source untouched. Failures during replacement attempt rollback and are retried by startup recovery when immediate rollback cannot complete.
