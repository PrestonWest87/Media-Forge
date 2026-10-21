# Configuration

GPU-Forge combines a local Compose environment file, a runtime JSON settings file, and environment variables used by the container. The shipped defaults are intended for a cautious first run: bulk processing is paused and operation mode is health-check only.

## Runtime files and state

| Host path | Container path | Purpose |
| --- | --- | --- |
| `.env` | Compose interpolation only | Host media paths, port binding, and image tag. Keep local; `.gitignore` excludes it. |
| `config/settings.json` | `/config/settings.json` | Persisted runtime settings saved by the dashboard/API. Optional; built-in defaults apply if absent. |
| `config/gpu_forge.db` | `/config/gpu_forge.db` | SQLite media metadata and job state, created on startup. |
| `config/gpu_forge.db-wal`, `config/gpu_forge.db-shm` | `/config/gpu_forge.db-wal`, `/config/gpu_forge.db-shm` | SQLite write-ahead logging files while the service is active. |

Create local files from the tracked examples:

```sh
cp .env.example .env
mkdir -p config
cp docs/examples/settings.example.json config/settings.json
```

Edit `.env` with real, existing absolute paths before starting Compose. Do not commit `.env`, runtime settings, databases, or logs. Back up `/config` separately if preserving the index and preferences is important. Media backups are stored beside source files and are not part of `/config`.

## Runtime settings

All fields are validated by the Pydantic `Settings` model. Unknown keys are rejected. If the settings file is absent, unreadable, malformed, or invalid, the application logs the issue and starts with built-in defaults.

| Setting | Accepted values | Default | Behavior |
| --- | --- | --- | --- |
| `operation_mode` | `health_check`, `transcode` | `health_check` | Health-check mode decodes but does not transcode. Transcode mode enables smart routing for eligible sources. |
| `backup_policy` | `auto_delete`, `keep` | `auto_delete` | Delete the original backup after a committed verified transcode, or retain it for manual restore/purge. Full Pipeline Test forces retention when a transcode applies. |
| `quality_profile` | `visually_lossless`, `balanced` | `visually_lossless` | NVENC CQ values are 19/20 for visually-lossless and 22/23 for balanced, selected by source height. |
| `nvenc_preset` | `p4`, `p6` | `p4` | NVENC preset used by the transcode command. |
| `media_roots` | Array of container paths | `[/media/movies, /media/tv]` | Roots recursively scanned for supported video files. They must match container mount destinations. |
| `gpu_index` | Integer ≥ 0 | `0` | Zero-based index among GPUs visible inside the container. A change restarts NVML monitoring. |
| `external_process_policy` | `known`, `any` | `known` | `known` recognizes external FFmpeg/Jellyfin processes; `any` accepts any external PID reported by NVML. |
| `external_gpu_utilization_threshold` | Integer 0–100 | `8` | Yield when an external process is present and the busiest compute/decode/encode engine is strictly above this percentage. |
| `thermal_pause_c` | Integer 70–90 | `89` | User thermal pause limit in °C. When available, the effective pause limit is the lower of this value and 5 °C below NVML's slowdown threshold, with a 60 °C floor. |
| `thermal_cooldown_sec` | Integer 0–300 | `30` | Required continuous dwell at or below 7 °C under the effective pause point before resuming. |

The web UI updates the visible settings immediately. `media_roots` and `gpu_index` are available through `PUT /api/settings`; the full settings object returned by `GET /api/settings` should be used as the update body. See the [API reference](api-reference.md).

## Smart route and bitrate floors

In normal transcode mode, the source codec must be one of `h264`, `mpeg2video`, `mpeg4`, `vc1`, `vp8`, or `vp9`. Its bitrate must be known and strictly greater than the applicable floor:

| Source height | Minimum bitrate floor |
| --- | ---: |
| 480p or lower | 600,000 bit/s |
| 481–720p | 1,100,000 bit/s |
| Above 720p or unknown | 2,200,000 bit/s |

HEVC and AV1 inputs receive a health check instead of another lossy encode. Sources with unsupported codecs/bit depth are skipped rather than software-decoded. Output is retained only if it saves at least 7% and passes metadata and full NVDEC verification. A Full Pipeline Test intentionally attempts an eligible codec regardless of the configured operation mode/bitrate floor, then still leaves the original untouched if savings are below 7%.

## Compose environment (`.env`)

| Variable | Example default | Purpose |
| --- | --- | --- |
| `GPU_FORGE_IMAGE` | `gpu-forge:local` | Compose image name/tag. Set to `weasts/media-forge:2.0.0` once an approved image release is available. |
| `GPU_FORGE_BIND_IP` | `127.0.0.1` | Host interface to which port 8090 is bound. |
| `GPU_FORGE_PORT` | `8090` | Host-side HTTP port. Container port remains 8090. |
| `GPU_FORGE_CONFIG_PATH` | `./config` | Persistent host directory mounted at `/config`. |
| `MEDIA_MOVIES_PATH` | Placeholder in `.env.example` | Existing, writable host movie directory, mounted at `/media/movies`. |
| `MEDIA_TV_PATH` | Placeholder in `.env.example` | Existing, writable host TV directory, mounted at `/media/tv`. |
| `TZ` | `Etc/UTC` | Container timezone. |

The `.env.example` values for media paths are deliberately placeholders. Replace them before running Compose. If a library root is not used, remove its mount and remove the corresponding path from `media_roots`.

The Compose service requests one NVIDIA GPU and enables `compute`, `video`, and `utility` driver capabilities. `NVIDIA_VISIBLE_DEVICES` defaults to `all`; set it in `.env` only if you need to restrict visible devices, then ensure `gpu_index` matches the resulting index.

## Container environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `GPU_FORGE_CONFIG_DIR` | `/config` | Directory containing `settings.json`. |
| `GPU_FORGE_DB_PATH` | `/config/gpu_forge.db` | SQLite database location. |
| `LOG_LEVEL` | `INFO` | Python logging level, e.g. `DEBUG`, `INFO`, or `WARNING`. |
| `NVIDIA_VISIBLE_DEVICES` | `all` in Compose | NVIDIA Container Toolkit device visibility. |
| `NVIDIA_DRIVER_CAPABILITIES` | `compute,video,utility` | Driver capabilities exposed inside the container. |

The service listens on `0.0.0.0:8090` inside the container. Compose controls host exposure; by default `.env.example` binds to loopback.

## Writable mounts and permissions

Media mounts must be writable. The worker creates temporary output and `.gpuforge_bak` files beside source files so verified replacements and restores can use same-directory atomic renames. The worker also copies source ownership and mode to a verified output. Make sure the container user/capabilities and host filesystem permit reads, writes, ownership preservation, and directory syncs.
