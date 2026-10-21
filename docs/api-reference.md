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
  "external_process_policy": "known",
  "external_gpu_utilization_threshold": 8,
  "thermal_pause_c": 89,
  "thermal_cooldown_sec": 30
}
```

Invalid JSON/settings returns `422 Unprocessable Entity`.

## Scan and queue controls

### `POST /api/scan`

Scans configured roots, upserts indexed rows, and enqueues pending work only when the bulk queue is enabled.

| Response field | Meaning |
| --- | --- |
| `scanned` | Supported media files encountered during this traversal. |
| `queued_candidates` | Rows submitted to the database upsert operation. |
| `enqueued` | Pending file IDs added to the active queue. |
| `total_files` | Number of indexed rows after the scan. |
| `queue_enabled` | Whether bulk processing is enabled. |

### `POST /api/queue/start`

Enables bulk processing and enqueues indexed rows with `PENDING` status.

Response shape: `{"ok": true, "queue_enabled": true, "enqueued": 12}`.

### `POST /api/queue/pause`

Disables new bulk work. A file already being processed is allowed to finish safely.

Response: `{"ok": true, "queue_enabled": false}`.

## Telemetry and file listing

### `GET /api/telemetry`

Returns:

- `state`, `gpu`, and `worker` runtime snapshots.
- `counts` by status, `total_files`, `files_checked`, `active_files`, and `progress_percent`.
- `saved_bytes` / `saved_gb` for completed verified replacements.
- `held_backup_bytes` / `held_backup_gb` for retained backup files still present.

The `gpu` object includes NVML availability/error, GPU index, core/NVDEC/NVENC utilization, VRAM, temperature/thermal thresholds, external PID/activity information, and GPU state. The `worker` object includes queue state, current path/phase, progress/FPS, and queued count.

### `GET /api/files`

Lists file records by status filter, with optional text search and pagination.

| Query | Default | Constraints |
| --- | --- | --- |
| `filter` | `active` | `active`, `corrupt`, `transcoded`, `skipped`, `unsupported`, or `failed`. |
| `q` | Empty | Matches filename or path; max length 300. |
| `limit` | `250` | Integer 1–500. |
| `offset` | `0` | Integer ≥ 0. |

Status mapping:

| Filter | Included statuses |
| --- | --- |
| `active` | `PENDING`, `CHECKING`, `TRANSCODING`, `VERIFYING_OUTPUT`, `PAUSED_YIELD` |
| `corrupt` | `CORRUPT` |
| `transcoded` | `TRANSCODED`, `TRANSCODED_BACKUP_KEPT` |
| `skipped` | `HEALTHY`, `SKIPPED_OPTIMAL` |
| `unsupported` | `SKIPPED_HW_UNSUPPORTED` |
| `failed` | `TRANSCODE_FAILED_RESTORED` |

The response contains `files`, `filter`, `limit`, and `offset`. File records include persisted path, source/output size, codec/stream metadata, status, error text, and timestamps. The endpoint adds `actual_size_bytes` and `has_backup` for the dashboard.

An unknown filter returns `400 Bad Request`.

## File and backup actions

### `POST /api/files/{file_id}/test`

Queues a selected-file NVDEC-only test; no transcode is attempted. It can run while bulk processing is paused. A file with a retained backup cannot be tested.

### `POST /api/files/{file_id}/full-test`

Queues source NVDEC verification, applicable H.265 transcode, output metadata and full NVDEC verification, and retained-backup replacement. It forces transcode mode for an eligible codec regardless of global mode. HEVC/AV1 and ineligible sources only receive the health check.

Both test actions return `404` for an unknown/missing file and `409` if the file has a backup, is already active, or already has a selected test queued.

### `POST /api/files/{file_id}/retry`

Resets an eligible row to `PENDING`. It is enqueued immediately only if the bulk queue is enabled. A verified transcode or existing backup returns `409`; an unknown ID returns `404`.

### `POST /api/files/{file_id}/restore`

Restores the retained original for a `TRANSCODED_BACKUP_KEPT` row. If the source was converted to Matroska, its original filename/extension is restored. Returns `409` if there is no retained backup, `404` if the row/backup is missing, and `500` if the file swap fails.

### `POST /api/backups/purge`

Permanently deletes existing backups for `TRANSCODED_BACKUP_KEPT` rows. Response contains arrays of successfully purged paths and failures. This operation is destructive; the UI asks for confirmation before calling it.

## Example requests

```sh
curl http://127.0.0.1:8090/api/health
curl -X POST http://127.0.0.1:8090/api/scan
curl 'http://127.0.0.1:8090/api/files?filter=active&limit=100&offset=0'
curl -X POST http://127.0.0.1:8090/api/queue/start
curl -X POST http://127.0.0.1:8090/api/queue/pause
```

## Common errors

| Status | Typical cause |
| --- | --- |
| `400` | Unknown file-list filter. |
| `404` | Unknown file ID or missing source/backup path. |
| `409` | Action conflicts with state, such as processing a verified replacement or a file with a retained backup. |
| `422` | Invalid settings body or query constraint. |
| `500` | A file operation or selected-test queue operation failed; inspect service logs and file error details. |
