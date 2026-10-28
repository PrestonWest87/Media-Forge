"""Metadata-based processing recommendations and transcode routing."""

from __future__ import annotations

from typing import Any

from app.config import Settings

TRANSCODE_CODECS = {"h264", "mpeg2video", "vc1", "mpeg4", "vp8", "vp9"}
UPSCALE_MAX_WIDTH = 720
UPSCALE_MAX_HEIGHT = 360
UPSCALE_CONTAINERS = {"avi", "m2ts", "m4v", "mkv", "mov", "mp4", "ts", "webm"}
UPSCALE_TARGET_HEIGHTS = (720, 1080, 1440)


def efficiency_floor(height: int | None) -> int:
    if height is None:
        return 2_200_000
    if height <= 480:
        return 600_000
    if height <= 720:
        return 1_100_000
    return 2_200_000


def is_transcode_candidate(info: dict[str, Any]) -> bool:
    codec = str(info.get("codec_name") or "").lower()
    bit_rate = info.get("bit_rate")
    if codec not in TRANSCODE_CODECS or bit_rate is None:
        return False
    try:
        return int(bit_rate) > efficiency_floor(info.get("height"))
    except (TypeError, ValueError):
        return False


def route_media(info: dict[str, Any], settings: Settings) -> str:
    """Return the health-only or transcode route from source metadata."""

    codec = str(info.get("codec_name") or "").lower()
    if settings.operation_mode != "transcode" or codec in {"hevc", "av1"}:
        return "health"
    return "transcode" if is_transcode_candidate(info) else "health"


def recommend_action(info: dict[str, Any]) -> tuple[str, str]:
    """Classify the next useful action independently of the active queue mode."""

    try:
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0

    if info.get("tagged_upscaled"):
        return "LEAVE_ALONE", "Already tagged by Upscale-Forge."
    if (
        0 < width <= UPSCALE_MAX_WIDTH
        and 0 < height <= UPSCALE_MAX_HEIGHT
        and not bool(info.get("tagged_upscaled"))
        and str(info.get("container_type") or "").casefold() in UPSCALE_CONTAINERS
    ):
        return (
            "UPSCALE",
            "Fits Upscale-Forge's 720×360 manual target; source frame timestamps and A/V sync are verified.",
        )
    codec = str(info.get("codec_name") or "").casefold()
    pixel_format = str(info.get("pix_fmt") or "").casefold()
    if codec == "h264" and ("10le" in pixel_format or "10be" in pixel_format):
        return "LEAVE_ALONE", "10-bit H.264 is outside the supported NVDEC processing path."
    if is_transcode_candidate(info):
        return "TRANSCODE", "Codec and bitrate qualify for smart H.265 transcoding."
    return "LEAVE_ALONE", "No eligible upscale or smart-transcode action was detected."
