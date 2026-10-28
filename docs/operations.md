# Deployment and operations

## Host prerequisites

1. Install Docker Engine and the Docker Compose plugin on a Linux host.
2. Install a compatible NVIDIA driver and NVIDIA Container Toolkit, then configure Docker to use the NVIDIA runtime/toolkit.
3. Confirm the GPU is working on the host and visible in a test container before starting GPU-Forge.
4. Choose persistent configuration storage and writable media directories. The container must be able to read and write every configured media root.

The image uses Python 3.12 on Debian Bookworm and builds FFmpeg 8.1.2 with `nv-codec-headers` 13.0.19.1. NVDEC, `hevc_nvenc`, and FFmpeg's native `aac` encoder are verified in the image build without GPL or nonfree FFmpeg flags; the matching FFmpeg/header source archives and build configuration are included under `/usr/share/doc/gpu-forge/ffmpeg/`. The NVIDIA driver is supplied by the host's NVIDIA Container Toolkit. Supported decoding/encoding depends on GPU generation, codec profile/pixel format, and driver compatibility.

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

1. Wait for the automatic startup scan or click **Scan Library**. Scans probe new/changed files for codec, container, bitrate, and resolution; unchanged files reuse saved metadata.
2. Confirm GPU telemetry is available and inspect the visible GPU index.
3. Select one file and run **NVDEC-only Test**. This checks hardware decoding without changing the file.
4. For an applicable source, run **Full Pipeline Test** to validate source decode, H.265 encode, output checks, and backup retention.
5. Select **Check + Smart H.265 Transcode** only when encoding is intended. Built-in settings default to **Health Check Only**.
6. Start each workflow explicitly: **Start Upscale Queue** to upscale all eligible low-resolution candidates at the selected target, **Start Video Queue** for video health/transcode work, and **Start Audio Queue** to add AAC compatibility tracks. All bulk queues start paused intentionally.

Selected-file tests and manual upscales can run while all bulk queues are paused. Each queue has its own start/pause control; pausing one lets its current file finish safely and does not start or pause the others. Running a scan enqueues new items only for the corresponding queue when it is enabled.

## Dashboard and queue behavior

The dashboard shows core/NVDEC/NVENC utilization, VRAM, temperature, independent upscale/video/audio queue state, overall progress, current phase/progress, saved bytes, retained-backup size, file statuses, audio compatibility status, scan-time codec/container/resolution, action recommendations, the ordered **Next up** list, and exact processing errors. `Transcode` identifies supported codecs above the bitrate floor; `Leave alone` indicates no video transcode/upscale route; `Upscale candidate` identifies untagged files at or below 720×360. Choose a target resolution and click **Start Upscale Queue** to enqueue all eligible candidates, or select one candidate and click **Upscale Selected** for a single manual job. Manual upscales go ahead of the bulk upscale queue. Select a queued or running upscale and click **Cancel Upscale** to stop it; the original remains unchanged and the cancelled candidate can be queued again.

The file progress bar switches to an indeterminate **Preparing AI inference engine** phase while VapourSynth/TensorRT loads or builds its engine. Once frames begin encoding, the bar switches to frame-based progress starting at 0%; elapsed phase time and FPS are shown alongside it. A live preview shows the source and upscaled versions of the same sampled frame, refreshed every three encoded frames. Preview JPEGs are temporary: the previous pair is removed as soon as its replacement is displayed, and remaining files are cleared when the job or preview ends.

The service uses one worker and does not run jobs concurrently. Dispatch priority is individual upscales, bulk upscales, selected-file tests, video/health work, then audio compatibility. The three bulk queues have independent start/pause controls. The audio queue copies video and existing audio, adds one AAC-LC stereo track for the first default audio stream (or first stream if there is no default), and marks that fallback as the default audio stream. It does not invoke the H.265 encoder. Jellyfin library refresh is performed separately by the operator after the files are updated. Processing phases include `INITIALIZING_UPSCALE_MODEL`, `PREPARING_UPSCALE_SEGMENTS`, `UPSCALING`, `FINALIZING_UPSCALE`, `VERIFYING_UPSCALE`, `CANCELLING_UPSCALE`, `CHECKING`, `TRANSCODING`, `AUDIO_REMUXING`, `AUDIO_VERIFYING`, and `VERIFYING_OUTPUT`. Active cancellation terminates only the Upscale-Forge job's process groups and is rejected once the verified replacement has begun. Thermal protection pauses after the configured trigger dwell at the effective cutoff (93°C / 5 seconds in this configuration), then resumes after cooling below its hysteresis point and the 15-second cool-down dwell. External yield watches any GPU process over the configured engine threshold, leaving other GPU services priority. `PAUSED_YIELD` is shown only while a live FFmpeg child is actually stopped; it resumes from the same phase when GPU/thermal conditions clear.

Audio results are shown independently from the video status: `PENDING`, `FAILED`, and `BLOCKED_BACKUP` items are considered by the audio queue; `NO_AUDIO` and `COMPATIBLE` need no replacement; `FALLBACK_ADDED` means the AAC track was installed; `FALLBACK_BACKUP_KEPT` means the audio operation retained its prior file.

When planning both video and audio work with `backup_policy=keep`, start video first. The audio queue can then update that video output while preserving its retained original backup. If audio runs first and creates a retained backup, a later video replacement is intentionally blocked until the backup is restored or purged.

Common statuses:

| Status | Meaning |
| --- | --- |
| `PENDING` | Indexed and waiting for work. |
| `HEALTHY` | Full source NVDEC check passed without a transcode. |
| `TRANSCODED` | H.265 output passed verification and the original backup was removed. |
| `TRANSCODED_BACKUP_KEPT` | Verified replacement installed and original retained. |
| `UPSCALE_QUEUED` / `UPSCALING` | Manual or bulk Upscale-Forge request waiting or running at upscale priority, with its chosen target resolution. |
| `UPSCALE_CANCEL_REQUESTED` | Active upscale is stopping after a user cancellation request. |
| `UPSCALE_CANCELLED` | Upscale stopped before source replacement; the original remains unchanged. |
| `UPSCALED_BACKUP_KEPT` | Upscale-Forge replaced the source after verification; original is retained for manual restore/purge. |
| `UPSCALE_FAILED` | Manual upscale failed; the original remains or is available in its retained backup. |
| `SKIPPED_OPTIMAL` | An attempted transcode saved less than 7%; the original was left untouched. Sources below smart-route bitrate floors receive a health check and are reported as `HEALTHY`. |
| `SKIPPED_HW_UNSUPPORTED` | Required codec, bit depth, GPU, or driver hardware path is unsupported. No software video fallback is used. |
| `CORRUPT` | Source metadata or decode indicates an unreadable/damaged input. |
| `TRANSCODE_FAILED_RESTORED` | Transcode failed and original stayed in place or was restored. |
| `PAUSED_YIELD` | A live FFmpeg child is paused for external GPU activity or thermal control; this state resumes automatically and is not terminal. |

## Backup, restore, and purge

Every candidate output is first written as a temporary sibling file. During a swap the original is renamed to `<original-path>.gpuforge_bak`; the verified output is then installed. Backup behavior after success:

- `auto_delete` removes the original backup after the verified replacement is committed.
- `keep` retains the original and marks the row `TRANSCODED_BACKUP_KEPT`.
- Audio-only replacements follow the same policy and report a retained prior file as `audio_status=FALLBACK_BACKUP_KEPT`; if a video backup already exists, the audio update preserves it.
- If automatic cleanup fails after the replacement is committed, the backup is retained and exposed rather than silently discarded.
- Full Pipeline Test forces backup retention if it transcodes a file.

Use **Restore Selected Backup** to restore a retained video/upscale original or the prior file from an audio-only replacement. Legacy formats are restored to their original filename/extension. Upscale backups are always retained; **Purge Verified Backups** is the only way to remove them. Purge is permanent, and the UI asks for confirmation.

Backups live beside media and are excluded from ordinary scans. On startup, recovery removes abandoned temporary outputs, restores prior media for incomplete transactions, cleans committed auto-delete backups left by a crash, reconciles stale database backup references, and requeues safe interrupted work. Avoid manually deleting `.gpuforge_bak` or `.gpuforge_audio_bak` artifacts until recovery has completed.

## GPU scheduling and temperature

NVML is polled approximately once per second. For external-work yielding, an external PID must be detected and the busiest core/decode/encode engine must exceed the configured threshold. `known` recognizes external FFmpeg/Jellyfin processes; `any` considers any PID returned by NVML. When a new external PID appears during GPU-Forge work, the watchdog briefly pauses its own FFmpeg child to sample external utilization independently.

Only GPU-Forge's registered FFmpeg children are signaled. External processes are never stopped by this service. After confirmed external GPU activity, resumption requires a 15-second low-utilization cooldown. A newly observed PID is first probed while the internal worker is paused; if that probe shows no meaningful external load, work can resume immediately. Thermal protection pauses at the lower of the configured limit and 5 °C below NVML's slowdown threshold, then resumes at least 7 °C cooler after the configured dwell period.

## Logs and maintenance

```sh
docker compose ps
docker compose logs --tail=200 gpu-forge
docker compose logs -f gpu-forge
docker compose -f ../upscaler/docker-compose.yml logs --tail=200 upscale-forge
docker compose -f ../upscaler/docker-compose.yml logs -f upscale-forge
```

Both services write to Docker stdout and rotating files in the shared config directory: `/config/gpu-forge.log` and `/opt/upscale-forge/config/upscale-forge.log`. Each file keeps five 10 MB rotations. Set `GPU_FORGE_LOG_LEVEL=DEBUG` or `UPSCALE_FORGE_LOG_LEVEL=DEBUG` in the corresponding Compose environment for subprocess commands and detailed preview/progress diagnostics. Logs include job/request IDs, phase transitions, frame progress, worker process IDs, subprocess durations/exit codes, GPU hold reasons, verification outcomes, and cleanup/replacement steps. GPU-Forge uses a single Uvicorn worker because its in-process queue and watchdog are not distributed across processes.

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
| A bulk queue is paused | Start the desired Upscale, Video, or Audio queue after scanning. Each queue pause finishes its current job and prevents additional jobs in that queue. Selected-file tests remain usable. |
| Audio status is `BLOCKED_BACKUP` | Resolve the untracked `.gpuforge_bak` collision named in `audio_error`, then start the audio queue again. |
| GPU work repeatedly pauses | Review GPU utilization/temperature and external/thermal thresholds. Pauses resume automatically; restarting the queue also recovers orphaned yield states. `known` process detection depends on NVML and Linux `/proc` visibility. |
| A backup remains after success | This is expected for `keep` or Full Pipeline Test. Otherwise inspect the row error; automatic backup cleanup can fail and preserve the original. |

## Network exposure

The UI/API currently has no built-in authentication. Prefer loopback plus an authenticated reverse proxy, or restrict the host port to a trusted network. The Uvicorn command trusts forwarded headers from all peers, so direct access to the container port should be limited to trusted clients/proxies. See [`SECURITY.md`](../SECURITY.md).
