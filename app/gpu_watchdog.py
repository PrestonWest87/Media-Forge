"""NVML telemetry and GPU-yield controller for internal FFmpeg children."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from typing import Any

try:
    import pynvml
except ImportError:  # pragma: no cover - exercised in CPU-only development environments
    pynvml = None

from app.config import SettingsStore

logger = logging.getLogger(__name__)
YieldCallback = Callable[[bool, str | None], Awaitable[None]]


class GpuWatchdog:
    """Poll NVML once per second and stop only this service's FFmpeg PIDs."""

    def __init__(self, settings_store: SettingsStore, on_yield: YieldCallback | None = None) -> None:
        self.settings_store = settings_store
        self.on_yield = on_yield
        self.internal_pids: set[int] = set()
        self.device: Any = None
        self.available = False
        self.error: str | None = None
        self.yielding = False
        self._yield_reason: str | None = None
        self._external_yielding = False
        self._thermal_yielding = False
        self._yield_probe = False
        self._yield_probe_started: float | None = None
        self._observed_external_pids: set[int] = set()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._nvml_initialized = False
        self._external_cooldown_started: float | None = None
        self._thermal_cooldown_started: float | None = None
        self._telemetry: dict[str, Any] = {
            "available": False,
            "error": None,
            "gpu_index": 0,
            "core_utilization": 0,
            "decoder_utilization": 0,
            "encoder_utilization": 0,
            "vram_used_mb": 0,
            "vram_total_mb": 0,
            "temperature_c": None,
            "external_pids": [],
            "external_gpu_utilization": 0,
            "external_gpu_utilization_threshold": 8,
            "external_busy": False,
            "thermal_slowdown_threshold_c": None,
            "thermal_shutdown_threshold_c": None,
            "thermal_pause_threshold_c": 82,
            "thermal_resume_threshold_c": 75,
            "thermal_yielding": False,
            "thermal_cooldown_remaining_sec": 0,
            "yielding": False,
            "yield_reason": None,
        }

    async def start(self) -> None:
        settings = await self.settings_store.load()
        self.available = False
        self.device = None
        self.error = None
        self.yielding = False
        self._yield_reason = None
        self._external_yielding = False
        self._thermal_yielding = False
        self._yield_probe = False
        self._yield_probe_started = None
        self._observed_external_pids.clear()
        self._external_cooldown_started = None
        self._thermal_cooldown_started = None
        self._telemetry["gpu_index"] = settings.gpu_index
        self._telemetry["external_gpu_utilization_threshold"] = settings.external_gpu_utilization_threshold
        self._telemetry["thermal_slowdown_threshold_c"] = None
        self._telemetry["thermal_shutdown_threshold_c"] = None
        if pynvml is None:
            self.error = "nvidia-ml-py is not installed"
            self._telemetry.update(available=False, error=self.error)
            logger.error("GPU telemetry unavailable: %s", self.error)
        else:
            try:
                pynvml.nvmlInit()
                self._nvml_initialized = True
                self.device = pynvml.nvmlDeviceGetHandleByIndex(settings.gpu_index)
                self.available = True
                self._telemetry.update(available=True, error=None)
                self._read_hardware_thermal_thresholds()
            except Exception as exc:  # noqa: BLE001 - NVML raises driver/version-specific exception classes.
                self.error = f"NVML initialization failed: {exc}"
                self._telemetry.update(available=False, error=self.error)
                logger.error("GPU telemetry unavailable: %s", self.error)

        self._stop_event.clear()
        self._task = asyncio.create_task(self._poll_loop(), name="gpu-forge-nvml-watchdog")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            await self._task
            self._task = None
        self._signal_internal(signal.SIGCONT)
        if self.yielding:
            self.yielding = False
            self._yield_reason = None
            self._external_yielding = False
            self._thermal_yielding = False
            self._yield_probe = False
            self._yield_probe_started = None
            self._external_cooldown_started = None
            self._thermal_cooldown_started = None
            await self._notify_yield(False, None)
        if self._nvml_initialized and pynvml is not None:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                logger.debug("NVML shutdown failed", exc_info=True)
            self._nvml_initialized = False

    def register_child(self, pid: int) -> None:
        self.internal_pids.add(pid)
        if self.yielding:
            self._signal_pid(pid, signal.SIGSTOP)

    def unregister_child(self, pid: int) -> None:
        self.internal_pids.discard(pid)

    def snapshot(self) -> dict[str, Any]:
        result = dict(self._telemetry)
        result["internal_pids"] = sorted(self.internal_pids)
        result["yielding"] = self.yielding
        result["yield_reason"] = self._yield_reason
        result["gpu_state"] = (
            "UNAVAILABLE"
            if not self.available
            else "PAUSED_THERMAL"
            if self.yielding and self._yield_reason == "thermal"
            else "PAUSED_YIELD"
            if self.yielding
            else "ACTIVE"
            if self.internal_pids
            else "IDLE"
        )
        return result

    def _read_hardware_thermal_thresholds(self) -> None:
        if pynvml is None or self.device is None:
            return
        for constant_name, telemetry_name in (
            ("NVML_TEMPERATURE_THRESHOLD_SLOWDOWN", "thermal_slowdown_threshold_c"),
            ("NVML_TEMPERATURE_THRESHOLD_SHUTDOWN", "thermal_shutdown_threshold_c"),
        ):
            try:
                threshold = pynvml.nvmlDeviceGetTemperatureThreshold(
                    self.device,
                    getattr(pynvml, constant_name),
                )
                self._telemetry[telemetry_name] = int(threshold)
            except Exception:
                logger.debug("NVML temperature threshold %s is unavailable", constant_name, exc_info=True)

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            cycle_started = time.monotonic()
            if self.available:
                await self._poll_once()
            remaining = max(0.0, 1.0 - (time.monotonic() - cycle_started))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
            except TimeoutError:
                pass

    async def _poll_once(self) -> None:
        if pynvml is None or self.device is None:
            return

        self._read_telemetry()
        settings = await self.settings_store.load()
        now = time.monotonic()
        external = self._external_gpu_pids(settings.external_process_policy)
        external_utilization = max(
            int(self._telemetry.get("core_utilization", 0)),
            int(self._telemetry.get("decoder_utilization", 0)),
            int(self._telemetry.get("encoder_utilization", 0)),
        )
        self._telemetry.update(
            {
                "external_pids": sorted(external),
                "external_gpu_utilization": external_utilization,
                "external_gpu_utilization_threshold": settings.external_gpu_utilization_threshold,
                "external_busy": False,
            }
        )
        utilization_above_threshold = bool(external) and self.exceeds_yield_threshold(
            int(self._telemetry.get("core_utilization", 0)),
            int(self._telemetry.get("decoder_utilization", 0)),
            int(self._telemetry.get("encoder_utilization", 0)),
            settings.external_gpu_utilization_threshold,
        )
        new_external = external - self._observed_external_pids
        self._observed_external_pids.intersection_update(external)

        # External workload state is evaluated independently from thermal
        # state. While our FFmpeg child is active, aggregate GPU utilization
        # includes its own work; newly appearing external PIDs are sampled
        # with the internal child stopped before counting them as busy.
        if self._external_yielding:
            if self._yield_probe:
                if (
                    self._yield_probe_started is not None
                    and now - self._yield_probe_started < 2.0
                ):
                    pass
                elif utilization_above_threshold:
                    self._yield_probe = False
                    self._yield_probe_started = None
                    self._external_cooldown_started = None
                    self._telemetry["external_busy"] = True
                else:
                    # The fresh external-only sample is below threshold. Treat
                    # this PID as ambient (e.g. a light camera decode) and
                    # resume immediately, without the external-work cooldown.
                    self._external_yielding = False
                    self._yield_probe = False
                    self._yield_probe_started = None
                    self._external_cooldown_started = None
                    self._observed_external_pids.update(external)
            elif utilization_above_threshold:
                self._external_cooldown_started = None
                self._telemetry["external_busy"] = True
            else:
                # Keep the existing 15-second external-app hysteresis.
                if self._external_cooldown_started is None:
                    self._external_cooldown_started = now
                elif now - self._external_cooldown_started >= 15.0:
                    self._external_yielding = False
                    self._external_cooldown_started = None
                    self._yield_probe_started = None
                    self._observed_external_pids.update(external)
        elif not self.internal_pids:
            # With no internal child, the NVML engine sample is external-only
            # and can be used directly to establish ambient GPU users.
            self._observed_external_pids = set(external)
            if external and utilization_above_threshold:
                self._external_yielding = True
                self._external_cooldown_started = None
                self._telemetry["external_busy"] = True
        elif new_external:
            # A newly appearing external PID may be starting a GPU workload.
            # Probe it with the internal process stopped so our own decoder
            # utilization cannot trip the external-work threshold.
            self._external_yielding = True
            self._yield_probe = True
            self._yield_probe_started = time.monotonic()
            self._external_cooldown_started = None

        # The card reports a slowdown limit of 94 C and shutdown at 97 C.
        # Pause at the lower of the user limit or five degrees below the NVML
        # slowdown point, then require a cool dwell before allowing more work.
        slowdown_c = self._telemetry.get("thermal_slowdown_threshold_c")
        pause_c = settings.thermal_pause_c
        if slowdown_c is not None:
            pause_c = min(pause_c, int(slowdown_c) - 5)
        pause_c = max(60, int(pause_c))
        resume_c = max(50, pause_c - 7)
        temperature_c = self._telemetry.get("temperature_c")

        if not self._thermal_yielding:
            if temperature_c is not None and temperature_c >= pause_c:
                self._thermal_yielding = True
                self._thermal_cooldown_started = None
                logger.warning(
                    "Thermal yield engaged at %s C (pause %s C, resume at or below %s C)",
                    temperature_c,
                    pause_c,
                    resume_c,
                )
        elif temperature_c is None or temperature_c > resume_c:
            self._thermal_cooldown_started = None
        elif self._thermal_cooldown_started is None:
            self._thermal_cooldown_started = now
        elif now - self._thermal_cooldown_started >= settings.thermal_cooldown_sec:
            self._thermal_yielding = False
            self._thermal_cooldown_started = None
            logger.info("Thermal cool-down complete at %s C", temperature_c)

        thermal_cooldown_remaining = 0
        if self._thermal_yielding:
            thermal_cooldown_remaining = settings.thermal_cooldown_sec
            if self._thermal_cooldown_started is not None:
                thermal_cooldown_remaining = max(
                    0,
                    int(settings.thermal_cooldown_sec - (now - self._thermal_cooldown_started)),
                )
        self._telemetry.update(
            {
                "thermal_pause_threshold_c": pause_c,
                "thermal_resume_threshold_c": resume_c,
                "thermal_yielding": self._thermal_yielding,
                "thermal_cooldown_remaining_sec": thermal_cooldown_remaining,
            }
        )

        should_pause = self._external_yielding or self._thermal_yielding
        reason = "thermal" if self._thermal_yielding else "external" if self._external_yielding else None
        if should_pause and not self.yielding:
            self.yielding = True
            self._yield_reason = reason
            self._signal_internal(signal.SIGSTOP)
            await self._notify_yield(True, reason)
        elif should_pause and reason != self._yield_reason:
            self._yield_reason = reason
            await self._notify_yield(True, reason)
        elif not should_pause and self.yielding:
            self._signal_internal(signal.SIGCONT)
            self.yielding = False
            self._yield_reason = None
            await self._notify_yield(False, None)

    @staticmethod
    def exceeds_yield_threshold(core: int, decoder: int, encoder: int, threshold: int) -> bool:
        """Use the busiest NVML engine and require it to exceed the configured threshold."""

        return max(core, decoder, encoder) > threshold

    def _read_telemetry(self) -> None:
        assert pynvml is not None
        try:
            utilization = pynvml.nvmlDeviceGetUtilizationRates(self.device)
            self._telemetry["core_utilization"] = int(utilization.gpu)
        except Exception:
            logger.debug("Unable to read GPU core utilization", exc_info=True)

        for function_name, key in (
            ("nvmlDeviceGetDecoderUtilization", "decoder_utilization"),
            ("nvmlDeviceGetEncoderUtilization", "encoder_utilization"),
        ):
            try:
                function = getattr(pynvml, function_name)
                utilization, _sampling_period = function(self.device)
                self._telemetry[key] = int(utilization)
            except Exception:  # noqa: BLE001 - NVML metric support varies by driver and GPU.
                self._telemetry[key] = 0

        try:
            memory = pynvml.nvmlDeviceGetMemoryInfo(self.device)
            self._telemetry["vram_used_mb"] = int(memory.used / (1024 * 1024))
            self._telemetry["vram_total_mb"] = int(memory.total / (1024 * 1024))
        except Exception:
            logger.debug("Unable to read GPU memory information", exc_info=True)

        try:
            self._telemetry["temperature_c"] = int(
                pynvml.nvmlDeviceGetTemperature(self.device, pynvml.NVML_TEMPERATURE_GPU)
            )
        except Exception:  # noqa: BLE001 - temperature may be unsupported by the selected device.
            self._telemetry["temperature_c"] = None

    def _external_gpu_pids(self, policy: str) -> set[int]:
        if pynvml is None:
            return set()
        pids: set[int] = set()
        for function_name in (
            "nvmlDeviceGetComputeRunningProcesses",
            "nvmlDeviceGetGraphicsRunningProcesses",
        ):
            function = getattr(pynvml, function_name, None)
            if function is None:
                continue
            try:
                processes = function(self.device)
            except Exception:
                # Some GPUs/drivers do not expose one of these process lists.
                logger.debug("NVML process query %s is unavailable", function_name, exc_info=True)
                continue
            for process in processes:
                pid = int(process.pid)
                if pid <= 0 or pid in self.internal_pids:
                    continue
                if policy == "any" or self._is_known_external(pid):
                    pids.add(pid)
        return pids

    @staticmethod
    def _is_known_external(pid: int) -> bool:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as command_line:
                command = command_line.read(8192).replace(b"\0", b" ").decode(errors="ignore").lower()
            with open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="ignore") as process_name:
                name = process_name.read(256).lower()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            return False
        identity = f"{command} {name}"
        return "ffmpeg" in identity or "jellyfin" in identity

    def _signal_internal(self, sig: signal.Signals) -> None:
        for pid in tuple(self.internal_pids):
            self._signal_pid(pid, sig)

    @staticmethod
    def _signal_pid(pid: int, sig: signal.Signals) -> None:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.error("Permission denied sending %s to internal FFmpeg PID %s", sig.name, pid)
        except OSError:
            logger.warning("Unable to send %s to internal FFmpeg PID %s", sig.name, pid, exc_info=True)

    async def _notify_yield(self, paused: bool, reason: str | None) -> None:
        if self.on_yield is None:
            return
        try:
            await self.on_yield(paused, reason)
        except Exception:
            logger.exception("GPU yield callback failed")
