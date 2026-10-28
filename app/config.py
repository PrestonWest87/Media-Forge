"""Persistent runtime settings for GPU-Forge."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class Settings(BaseModel):
    """User-configurable controls exposed by the dashboard."""

    model_config = ConfigDict(extra="forbid")

    operation_mode: Literal["health_check", "transcode"] = "health_check"
    backup_policy: Literal["auto_delete", "keep"] = "auto_delete"
    quality_profile: Literal["visually_lossless", "balanced"] = "visually_lossless"
    nvenc_preset: Literal["p4", "p6"] = "p4"
    media_roots: list[str] = Field(default_factory=lambda: ["/media/movies", "/media/tv"])
    gpu_index: int = Field(default=0, ge=0)
    external_process_policy: Literal["known", "any"] = "known"
    external_gpu_utilization_threshold: int = Field(default=8, ge=0, le=100)
    thermal_pause_c: int = Field(default=89, ge=70, le=95)
    thermal_trip_dwell_sec: int = Field(default=5, ge=0, le=60)
    thermal_cooldown_sec: int = Field(default=30, ge=0, le=300)


class SettingsStore:
    """Loads settings once per request and writes updates atomically."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        config_dir = Path(os.environ.get("GPU_FORGE_CONFIG_DIR", "/config"))
        self.path = Path(path) if path is not None else config_dir / "settings.json"
        self._lock = asyncio.Lock()
        self._settings: Settings | None = None

    async def load(self) -> Settings:
        async with self._lock:
            if self._settings is not None:
                return self._settings.model_copy(deep=True)

            try:
                payload = json.loads(await asyncio.to_thread(self.path.read_text, encoding="utf-8"))
                self._settings = Settings.model_validate(payload)
            except FileNotFoundError:
                self._settings = Settings()
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.error("Unable to read settings at %s; using defaults: %s", self.path, exc)
                self._settings = Settings()

            return self._settings.model_copy(deep=True)

    async def save(self, settings: Settings) -> Settings:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(self.path.name + ".tmp")
            encoded = json.dumps(settings.model_dump(mode="json"), indent=2) + "\n"
            await asyncio.to_thread(temp_path.write_text, encoded, encoding="utf-8")
            await asyncio.to_thread(os.replace, temp_path, self.path)
            self._settings = settings.model_copy(deep=True)
            return self._settings.model_copy(deep=True)
