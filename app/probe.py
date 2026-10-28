"""Shared FFprobe helpers used during scanning and media processing."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    parsed = _number(value)
    if parsed is None:
        return None
    return int(parsed)


def _container_type(format_name: str, file_path: str | None) -> str | None:
    formats = {item.strip().casefold() for item in format_name.split(",") if item.strip()}
    extension = Path(file_path).suffix.lstrip(".").casefold() if file_path else ""

    if "matroska" in formats or "webm" in formats:
        return "webm" if extension == "webm" else "mkv"
    if formats.intersection({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}):
        return extension if extension in {"mp4", "m4v", "mov", "3gp", "3g2", "mj2"} else "mp4"
    if "avi" in formats:
        return "avi"
    if "mpegts" in formats:
        return extension if extension in {"ts", "m2ts", "mts"} else "ts"
    if "mpeg" in formats:
        return extension if extension in {"mpeg", "mpg", "vob"} else "mpeg"
    if "asf" in formats:
        return "asf"
    if "flv" in formats:
        return "flv"
    if "ogg" in formats:
        return "ogv"
    if "mxf" in formats:
        return "mxf"
    if "vob" in formats:
        return "vob"
    if "3g2" in formats:
        return "3g2"
    if "3gp" in formats:
        return "3gp"
    return extension or next(iter(formats), None)


def parse_probe_data(payload: dict[str, Any], file_path: str | None = None) -> dict[str, Any] | None:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    valid_streams = [stream for stream in streams if isinstance(stream, dict)]
    video_streams = [stream for stream in valid_streams if stream.get("codec_type") == "video"]
    if not video_streams:
        return None

    video = video_streams[0]
    video_details = [
        {
            "index": _integer(stream.get("index")),
            "codec_name": stream.get("codec_name"),
            "pix_fmt": stream.get("pix_fmt"),
            "width": _integer(stream.get("width")),
            "height": _integer(stream.get("height")),
        }
        for stream in video_streams
    ]
    format_info = payload.get("format")
    if not isinstance(format_info, dict):
        format_info = {}
    duration = _number(format_info.get("duration"))
    if duration is None:
        duration = _number(video.get("duration"))
    bit_rate = _integer(video.get("bit_rate"))
    if bit_rate is None:
        bit_rate = _integer(format_info.get("bit_rate"))

    audio = [stream for stream in valid_streams if stream.get("codec_type") == "audio"]
    subtitles = [stream for stream in valid_streams if stream.get("codec_type") == "subtitle"]
    audio_details: list[dict[str, Any]] = []
    for stream in audio:
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        tag_values = {str(key).casefold(): value for key, value in tags.items()}
        disposition = stream.get("disposition")
        if not isinstance(disposition, dict):
            disposition = {}
        audio_details.append(
            {
                "index": _integer(stream.get("index")),
                "codec_name": stream.get("codec_name"),
                "profile": stream.get("profile"),
                "channels": _integer(stream.get("channels")),
                "channel_layout": stream.get("channel_layout"),
                "sample_rate": _integer(stream.get("sample_rate")),
                "language": str(tag_values["language"]) if "language" in tag_values else None,
                "title": str(tag_values.get("title") or tag_values.get("handler_name") or "") or None,
                "disposition": {
                    str(key): bool(_integer(value) or 0)
                    for key, value in disposition.items()
                },
            }
        )
    format_tags = format_info.get("tags") if isinstance(format_info.get("tags"), dict) else {}
    video_tags = video.get("tags") if isinstance(video.get("tags"), dict) else {}
    tagged_upscaled = any(
        str(key).casefold() == "upscale_forge"
        for tags in (format_tags, video_tags)
        for key in tags
    )
    format_name = str(format_info.get("format_name") or "")

    return {
        "codec_name": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "width": _integer(video.get("width")),
        "height": _integer(video.get("height")),
        "video_streams": len(video_streams),
        "video_stream_details": video_details,
        "duration": duration,
        "bit_rate": bit_rate,
        "audio_streams": len(audio),
        "audio_stream_details": audio_details,
        "sub_streams": len(subtitles),
        "video_index": _integer(video.get("index")) or 0,
        "subtitle_streams": [
            {"index": _integer(stream.get("index")), "codec_name": stream.get("codec_name")}
            for stream in subtitles
        ],
        "format_name": format_name,
        "container_type": _container_type(format_name, file_path),
        "tagged_upscaled": tagged_upscaled,
    }


def metadata_db_fields(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "codec": info.get("codec_name"),
        "pix_fmt": info.get("pix_fmt"),
        "container_type": info.get("container_type"),
        "format_name": info.get("format_name"),
        "tagged_upscaled": int(bool(info.get("tagged_upscaled"))),
        "bit_rate": info.get("bit_rate"),
        "width": info.get("width"),
        "height": info.get("height"),
        "audio_streams": int(info.get("audio_streams") or 0),
        "sub_streams": int(info.get("sub_streams") or 0),
        "duration": info.get("duration"),
    }


async def probe_file(
    file_path: str,
    *,
    ffprobe: str = "ffprobe",
    timeout: float = 120.0,
) -> tuple[dict[str, Any] | None, str | None]:
    """Probe a file without blocking the event loop; terminate a stuck probe safely."""

    try:
        process = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return None, f"Unable to execute ffprobe: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()
        return None, f"ffprobe exceeded the {timeout:g}-second probe timeout."
    except asyncio.CancelledError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()
        raise

    if process.returncode != 0:
        detail = stderr.decode(errors="replace") or f"ffprobe exited with code {process.returncode}."
        return None, detail
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"ffprobe returned invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "ffprobe returned an invalid JSON document."

    info = parse_probe_data(payload, file_path)
    if info is None:
        return None, "No video stream was found."
    return info, None
