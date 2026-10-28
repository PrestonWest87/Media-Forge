# API reference

GPU-Forge serves the dashboard and JSON API from one origin on port `8090`. Interactive OpenAPI documentation is at `/docs`, ReDoc at `/redoc`, and the schema at `/openapi.json`.

The API currently has no authentication. Restrict network access or use a trusted authenticated reverse proxy. The examples use `http://127.0.0.1:8090`.

## Health and settings

### `GET /api/health`

Returns API and NVML initialization status:

```json
{
  "ok": true,
  "gpu_available": true,
  "gpu_error": null
}
```

`ok` means the API is responding. Media hardware work requires `gpu_available: true`.

### `GET /api/settings`

Returns the current validated settings object. See [Configuration](configuration.md) for all fields and defaults.

### `PUT /api/settings`

Validates and persists the full settings object to `/config/settings.json` (or `GPU_FORGE_CONFIG_DIR`). Unknown keys are rejected. A changed `gpu_index` restarts NVML monitoring.

Example body:

```json
{
  "operation_mode": "health_check",
  "backup_policy": "auto_delete",
  "quality_profile": "visually_lossless",
  "nvenc_preset": "p4",
  "media_roots": ["/media/movies", "/media/tv"],
  "gpu_index": 0,
  "external_process_policy": "any",
  "external_gpu_utilization_threshold": 8,
  "thermal_pause_c": 93,
  "thermal_trip_dwell_sec": 5,
  "thermal_cooldown_sec": 15
}
```

Invalid JSON/settings returns `422 Unprocessable Entity`.

## Scan and queue controls

### `POST /api/scan`

Scans configured roots and upserts indexed rows. New video work is enqueued only when the video queue is enabled; pending audio and upscale work are enqueued only when their separate queues are enabled.

| Response field | Meaning |
| --- | --- |
| `scanned` | Supported media files encountered during this traversal. |
| `queued_candidates` | Rows submitted to the database upsert operation. |
| `probed` | Files whose saved fingerprint/metadata required an FFprobe run during this scan. |
| `probe_failed` | Files whose scan-time metadata probe failed; the worker retries before processing. |
| `action_counts` | Totals for `TRANSCODE`, `UPSCALE`, and `LEAVE_ALONE` recommendations. |
| `enqueued` | Pending file IDs added to the active queue. |
| `audio_enqueued` | Pending audio compatibility file IDs added to the audio queue. |
| `upscale_enqueued` | Eligible low-resolution candidates added to the bulk upscale queue. |
| `total_files` | Number of indexed rows after the scan. |
| `queue_enabled` | Whether bulk processing is enabled. |
| `upscale_queue_enabled` | Whether the separate bulk upscale queue is enabled. |
| `audio_queue_enabled` | Whether the separate audio compatibility queue is enabled. |

### `POST /api/queue/start`

Enables the video/health bulk queue and enqueues indexed rows with `PENDING` status. When smart transcode mode is active, untranscoded rows already marked `HEALTHY` with a `TRANSCODE` recommendation are also returned to `PENDING`. Orphaned yield/in-progress states are reconciled, while the currently active file is left alone. Verified transcodes remain terminal across rescans. This endpoint does not start audio compatibility work.

Response shape: `{"ok": true, "queue_enabled": true, "enqueued": 12}`.

### `POST /api/queue/pause`

Disables new bulk work. A file already being processed is allowed to finish safely.

Response: `{"ok": true, "queue_enabled": false}`.

### `POST /api/upscale-queue/start?target_height=1080`

Starts a separate bulk Upscale-Forge queue for all eligible `UPSCALE` candidates. `target_height` accepts `720`, `1080`, or `1440`, defaulting to `1080`. It does not start the video or audio queue. The worker prioritizes manual upscales, then bulk upscales, before video and audio bulk jobs.

Response shape: `{"ok": true, "upscale_queue_enabled": true, "target_height": 1080, "queued": 12}`. Returns `503` if Upscale-Forge is unavailable.

### `POST /api/upscale-queue/pause`

Pauses new bulk upscale work after the current upscale completes. Individually selected upscales remain independently user-triggered.

Response: `{"ok": true, "upscale_queue_enabled": false}`.

### `POST /api/audio-queue/start`

Enables the independent audio compatibility queue and enqueues media rows whose `audio_status` is `PENDING`, `FAILED`, or `BLOCKED_BACKUP`. It does not enable the video/health queue. Each job preserves video by stream copy, keeps existing audio streams, and adds one default AAC-LC stereo fallback for the first default audio stream (or first audio stream if none is marked default).

Response shape: `{"ok": true, "audio_queue_enabled": true, "enqueued": 12}`.

### `POST /api/audio-queue/pause`

Disables new audio bulk work; an audio job already in progress finishes safely.

Response: `{"ok": true, "audio_queue_enabled": false}`.

The single worker dispatches individually selected upscales, bulk upscales, selected-file tests, video/health work, then audio compatibility work. Each bulk queue has independent start/pause controls, and the worker serializes jobs rather than running multiple FFmpeg tasks concurrently.

## Telemetry and file listing

### `GET /api/telemetry`

Returns:

- `state`, `gpu`, and `worker` runtime snapshots, plus `audio_counts` by compatibility status.
- `counts` by status, `total_files`, `files_checked`, `active_files`, and `progress_percent`.
- `saved_bytes` / `saved_gb` for completed verified replacements.
- `held_backup_bytes` / `held_backup_gb` for retained backup files still present.

The `gpu` object includes NVML availability/error, GPU index, core/NVDEC/NVENC utilization, VRAM, temperature/thermal thresholds, external PID/activity information, and GPU state. The `worker` object includes independent `queue_enabled`, `upscale_queue_enabled`, and `audio_queue_enabled` flags; video, bulk-upscale, and audio queued counts; current path/phase/detail; progress/FPS; elapsed phase time; whether model initialization uses indeterminate progress; queued count; and `up_next`, an ordered preview across manual/bulk upscale, selected tests, video work, and audio work. During model initialization, `current_progress_indeterminate` is true; frame-based encode progress starts at 0 once the encoder emits its first frame.

### `GET /api/files`

Lists file records by status filter, with optional text search and pagination.

| Query | Default | Constraints |
| --- | --- | --- |
| `filter` | `active` | `active`, `audio_pending`, `corrupt`, `transcoded`, `upscaled`, `upscale_candidates`, `skipped`, `unsupported`, or `failed`. |
| `q` | Empty | Matches filename or path; max length 300. |
| `limit` | `250` | Integer 1–500. |
| `offset` | `0` | Integer ≥ 0. |

Status mapping:

| Filter | Included statuses |
| --- | --- |
| `active` | `PENDING`, `CHECKING`, `TRANSCODING`, `VERIFYING_OUTPUT`, `AUDIO_REMUXING`, `AUDIO_VERIFYING`, `AUDIO_TRANSCODING`, `AUDIO_RESTORING`, `PAUSED_YIELD`, `UPSCALE_QUEUED`, `UPSCALING`, `UPSCALE_CANCEL_REQUESTED` |
| `audio_pending` | Audio status `PENDING`, `PROCESSING`, `RESTORING`, `BLOCKED_BACKUP`, or `FAILED`. |
| `corrupt` | `CORRUPT` |
| `transcoded` | `TRANSCODED`, `TRANSCODED_BACKUP_KEPT` |
| `upscaled` | `UPSCALED`, `UPSCALED_BACKUP_KEPT` |
| `upscale_candidates` | Rows with the `UPSCALE` action tag, ready for a manual upscale selection. |
| `skipped` | `HEALTHY`, `SKIPPED_OPTIMAL` |
| `unsupported` | `SKIPPED_HW_UNSUPPORTED` |
| `failed` | `TRANSCODE_FAILED_RESTORED`, `UPSCALE_FAILED` |
| `cancelled` | `UPSCALE_CANCELLED` |

The response contains `files`, `filter`, `limit`, and `offset`. File records include persisted path, source/output size, codec, normalized `container_type`, raw FFprobe `format_name`, resolution, stream metadata, `audio_status` / `audio_error`, `action_tag` / `action_reason`, `transcoded_at`, `upscaled_at`, `upscale_target_height`, status, error text, and timestamps. Audio status values include `PENDING`, `PROCESSING`, `NO_AUDIO`, `COMPATIBLE`, `FALLBACK_ADDED`, `FALLBACK_BACKUP_KEPT`, `BLOCKED_BACKUP`, and `FAILED`. Action tags are `TRANSCODE`, `LEAVE_ALONE`, or `UPSCALE`; `UPSCALE` means the file fits Upscale-Forge's 720×360 manual candidate limit, not that it has been queued. The endpoint adds `actual_size_bytes` and `has_backup` for the dashboard.

An unknown filter returns `400 Bad Request`.

## File and backup actions

### `POST /api/files/{file_id}/test`

Queues a selected-file NVDEC-only test; no transcode is attempted. It can run while bulk processing is paused. A file with a retained backup cannot be tested.

### `POST /api/files/{file_id}/full-test`

Queues source NVDEC verification, applicable H.265 transcode, output metadata and full NVDEC verification, and retained-backup replacement. It forces transcode mode for an eligible codec regardless of global mode. HEVC/AV1 and ineligible sources only receive the health check.

Both test actions return `404` for an unknown/missing file and `409` if the file has a backup, is already active, or already has a selected test queued.

### `POST /api/files/{file_id}/retry`

Resets an eligible row to `PENDING`. It is enqueued immediately only if the bulk queue is enabled. A verified transcode or existing backup returns `409`; an unknown ID returns `404`.

### `POST /api/files/{file_id}/upscale`

Queues a manual Upscale-Forge request for a selected, untagged candidate at or below 720×360. The optional `target_height` query parameter accepts `720`, `1080`, or `1440` and defaults to `1080`. The GPU-Forge queue previews the selected target and places it at the front of all bulk work; it runs after the currently active file finishes. It works while the bulk queues are paused. Upscale-Forge must be running; its own idle/writer checks and timestamp/quality verification apply. VFR timestamps are preserved, and successful output retains the original as `.gpuforge_bak` until manual restore or purge.

Returns the priority-queue position and whether the bulk queue is enabled. Returns `409` when the file is not an eligible candidate, already active, already upscaled, or has a retained backup; returns `503` if the Upscale-Forge daemon/shared state database is unavailable.

### `POST /api/files/{file_id}/upscale/cancel`

Cancels a queued upscale immediately or requests cancellation of an active one. Active cancellation stops the job's owned processes and is serialized against the verified source replacement. The source remains unchanged when cancellation succeeds; the completed staging job is removed. A cancelled item can be selected and queued again.

The response contains `status` (`UPSCALE_CANCELLED` when stopped or `UPSCALE_CANCEL_REQUESTED` while an active job is shutting down) and `request_id`. Returns `404` for an unknown file, `409` when the upscale has already completed/failed or is not cancellable, and `503` when the shared Upscale-Forge state database cannot be reached.

### `GET /api/files/{file_id}/upscale/preview`

Returns the latest temporary before/after preview token and sampled segment/frame number while the manual upscale is active. Returns `{"available": false}` until the first preview is generated.

### `GET /api/files/{file_id}/upscale/preview/{preview_id}/{side}`

Serves one JPEG (`side` is `before` or `after`) for the sampled source/output frame pair.

### `POST /api/files/{file_id}/upscale/preview/{preview_id}/displayed`

Acknowledges that the dashboard has loaded the preview pair and prunes superseded JPEGs. The dashboard requests another pair every three encoded frames and releases the prior in-browser image URL after replacement.

### `POST /api/files/{file_id}/upscale/preview/cleanup`

Removes the request's remaining preview JPEGs when the dashboard stops displaying the active job. Upscale-Forge also removes previews when the job completes, fails, or is cancelled.

### `POST /api/files/{file_id}/restore`

Restores the retained original for a `TRANSCODED_BACKUP_KEPT` or `UPSCALED_BACKUP_KEPT` row, or the prior media for a row whose `audio_status` is `FALLBACK_BACKUP_KEPT`. If the source was converted to Matroska, its original filename/extension is restored. Returns `409` if there is no retained backup, `404` if the row/backup is missing, and `500` if the file swap fails.

### `POST /api/backups/purge`

Permanently deletes existing backups for `TRANSCODED_BACKUP_KEPT`, `UPSCALED_BACKUP_KEPT`, or audio compatibility rows with `audio_status=FALLBACK_BACKUP_KEPT`. Upscale backups are never removed automatically. Response contains arrays of successfully purged paths and failures. This operation is destructive; the UI asks for confirmation before calling it.

## Example requests

```sh
curl http://127.0.0.1:8090/api/health
curl -X POST http://127.0.0.1:8090/api/scan
curl 'http://127.0.0.1:8090/api/files?filter=active&limit=100&offset=0'
curl -X POST http://127.0.0.1:8090/api/queue/start
curl -X POST http://127.0.0.1:8090/api/queue/pause
curl -X POST http://127.0.0.1:8090/api/audio-queue/start
curl -X POST http://127.0.0.1:8090/api/audio-queue/pause
curl -X POST http://127.0.0.1:8090/api/files/123/upscale/cancel
```

## Common errors

| Status | Typical cause |
| --- | --- |
| `400` | Unknown file-list filter. |
| `404` | Unknown file ID or missing source/backup path. |
| `409` | Action conflicts with state, such as processing a verified replacement or a file with a retained backup. |
| `422` | Invalid settings body or query constraint. |
| `500` | A file operation or selected-test queue operation failed; inspect service logs and file error details. |
