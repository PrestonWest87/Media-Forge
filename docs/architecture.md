# Architecture

GPU-Forge is a single-process FastAPI service with one asynchronous media worker. Its lifespan initializes persistent state and startup recovery before enabling the scanner, NVML watchdog, and worker.

## Components

| Component | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI routes, service lifecycle, ordered startup recovery. |
| `app/config.py` | Pydantic settings model and atomic JSON settings persistence. |
| `app/db.py` | SQLite schema, media metadata/status queries, saved-space calculations. |
| `app/scanner.py` | Non-following traversal, scan-time FFprobe metadata, action recommendations, and batched indexing. |
| `app/probe.py` | Shared FFprobe execution/parsing and container normalization. |
| `app/routing.py` | Transcode eligibility and per-file action recommendations. |
| `app/upscale_client.py` | Manual request handoff to the sibling Upscale-Forge daemon through its shared SQLite state. |
| `app/worker.py` | Priority queue for manual and bulk upscales/tests, video work, and the independent audio compatibility bulk queue; FFmpeg command construction, verification, and replacement. |
| `app/gpu_watchdog.py` | NVML telemetry, external-process detection, thermal policy, internal FFmpeg pause/resume. |
| `app/recovery.py` | Abandoned temporary cleanup and repair/requeue of interrupted swaps/jobs. |
| `app/static/index.html` | Single-page dashboard and browser-side API client. |

## Startup sequence

1. Load settings, initialize SQLite/WAL, and create schema if absent.
2. Run recovery before new worker work. Recovery reconciles `.gpuforge_tmp`, `.gpuforge_bak`, and audio-remux rollback files with persisted row state.
3. Start the NVML watchdog and media worker. Video, bulk-upscale, and audio queues remain disabled.
4. Launch an asynchronous initial scan of configured `media_roots`.

The startup scan indexes files but does not enable any bulk queue. Selected-file tests and user-triggered upscales can run while the bulk queues are paused.

## Scanning and persistence

The scanner recursively uses `os.scandir`, does not follow file or directory symlinks, and considers these extensions case-insensitively:

`.3gp`, `.asf`, `.avi`, `.flv`, `.m2ts`, `.m2v`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.mpeg`, `.mpg`, `.mts`, `.mxf`, `.ogv`, `.ts`, `.vob`, `.webm`, `.wmv`.

Names containing `.gpuforge_tmp`, `.gpuforge_bak`, `.gpuforge_audio_bak`, or `.upscale_staging` are excluded from normal scans. For new or changed fingerprints, the scanner runs FFprobe and stores codec, normalized container type, resolution, bitrate, stream counts, duration, audio-work status, and Upscale-Forge output tags. Unchanged files reuse saved metadata, avoiding a full library reprobe on every scan. File path, size, and modification time detect changes; a changed fingerprint resets video status and audio status to `PENDING`.

Each row receives an action recommendation: `TRANSCODE` for a supported codec above its bitrate floor, `UPSCALE` for an untagged supported container at or below 720×360, and `LEAVE_ALONE` otherwise. Upscaling can be user-started for one selected candidate or through the separate bulk Upscale Queue, which applies the currently selected target to eligible candidates. The sibling Upscale-Forge scanner applies its age, write-open, and tag checks; its segment pipeline restores source presentation timestamps and verifies frame timing and A/V start-offset parity before replacement. Upscale candidates receive a health check only from the normal video queue and are not H.265-transcoded.

SQLite stores one row per tracked path, including codec/container metadata, action tag/reason, dimensions, bitrate, duration, audio/subtitle counts, independent audio compatibility status/error, backup path, output size, transcode/upscale timestamps and target, status, errors, and processing time. The committed output size/mtime and terminal transcode/upscale/audio status survive unchanged scans; the audio queue can be explicitly started to process pending audio work without enabling video transcoding. The default database path is `/config/gpu_forge.db`.

## Processing and routing

The worker checks that the file exists and asks `ffprobe` for stream metadata before media decoding. It rejects missing/empty sources, unreadable containers, missing video streams, and unsupported codec/bit-depth paths. Every media FFmpeg decode command must use:

- `-xerror` to fail when decoding reports an error.
- `-hwaccel cuda` and `-hwaccel_output_format cuda` to keep video decode on CUDA surfaces.
- The configured visible GPU index.

The service does not use software video decode/encode fallback. If GPU/NVML is unavailable or a source is outside the NVDEC whitelist, the job is marked unsupported without a software video decode.

In `health_check` mode, the worker performs a full NVDEC decode and does not transcode. Smart transcode mode requires one of `{h264, mpeg2video, mpeg4, vc1, vp8, vp9}` and a known bitrate strictly above the resolution floor: 0.6 Mbit/s up to 480p, 1.1 Mbit/s up to 720p, and 2.2 Mbit/s above 720p/unknown. HEVC/AV1 are health-check only. H.264 10-bit is explicitly unsupported by this decode path.

Video transcodes use `hevc_nvenc`, selected CQ profile/preset, and audio stream copy. Scan-time `TRANSCODE` tags are recommendations; the active operation mode still controls whether the video queue performs health checks only or encodes. Separately, the user-started audio queue selects only the first default audio stream (or the first stream when none is marked default), copies the video and all original audio, then adds one AAC-LC stereo compatibility track. The AAC track is marked default and copied original tracks are left selectable. A compatible AAC-LC mono/stereo source or an already-added fallback is not encoded again. MP4/M4V output uses an MP4-compatible container; other output uses Matroska. Legacy/non-target containers are installed with an `.mkv` filename if a safe destination is available. MP4/M4V subtitle mapping accepts `mov_text`; Matroska output maps subtitle and data streams for copy.

## Verification and atomic replacement

FFmpeg success alone is not enough to replace a source. The worker:

1. Requires a non-empty output and verifies source size/mtime did not change while processing.
2. For normal queue work, discards output that saves less than 7%.
3. For video transcodes, probes output and requires HEVC, duration within 0.5 seconds, identical audio stream count, and for MKV inputs identical subtitle count. For audio jobs, verifies the original audio streams remain, exactly one AAC fallback is present and default, and video codec/dimensions/duration are unchanged.
4. Runs an audio decode check for the remuxed streams and a full output NVDEC decode with `-xerror` before replacement.
5. Copies source ownership/mode to the output, syncs it, and performs same-directory atomic renames.
6. Commits the media row only after output installation; then deletes or retains the original backup per policy.

Temporary outputs are named `<source-path>.gpuforge_tmp<extension>`. Backups use `<original-path>.gpuforge_bak`. When output container changes, the old-extension backup remains beside the new `.mkv` destination. Existing backups and conflicting destinations are not overwritten.

Startup recovery prefers the prior media when an audio or video swap was not committed, keeps retained backups, removes safe leftover temporary/auto-clean artifacts, reconciles stale backup references, and requeues interrupted jobs where possible.

## Bulk queue ordering

The user starts and pauses bulk-upscale, video, and audio queues independently. They use the single worker and do not run FFmpeg jobs concurrently. Dispatch priority is individual upscales, bulk upscales, selected-file tests, video/health work, then audio compatibility. Starting one bulk queue does not start the others. Audio-only jobs copy video instead of running NVENC. A file with a retained backup from an earlier operation can be audio-remuxed while preserving that backup; an untracked backup collision is reported and left untouched.

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

CHECKING / TRANSCODING / VERIFYING_OUTPUT -> PAUSED_YIELD -> previous phase
```

`PAUSED_YIELD` is persisted only while a live FFmpeg child is stopped by the watchdog; telemetry distinguishes thermal pause state. It is a resumable phase, not a terminal result. Queue start reconciles yield/in-progress rows that no live worker owns, and startup recovery preserves completed output fingerprints when an old pause callback overwrote their terminal status.

Manual upscales are held in a higher-priority GPU-Forge queue ahead of selected tests and bulk transcode/health work. GPU-Forge submits the selected target (720p, 1080p, or 1440p) to Upscale-Forge using the shared SQLite `manual_jobs` table and waits for its terminal result before releasing the queue to the next item. Upscale-Forge's `scanner.auto_process` defaults to `false`; no library-wide upscaling is scheduled automatically. The original is retained as `<source-path>.gpuforge_bak` and is only removed by the user's **Purge Verified Backups** action.

Failures before replacement leave the source untouched. Failures during replacement attempt rollback and are retried by startup recovery when immediate rollback cannot complete.
