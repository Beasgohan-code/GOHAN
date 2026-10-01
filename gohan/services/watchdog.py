"""Self-healing watchdog.

A background sweep, run every ``WATCHDOG_INTERVAL`` seconds, that keeps a
long-running bot healthy on a small box:

1. 🧹 **Files** - delete temp downloads older than ``CLEANUP_AFTER_HOURS``.
   A directory is only removed when its *newest* file is old, so work in
   progress is never deleted.
2. 💽 **Disk** - below ``MIN_FREE_DISK_GB`` it sweeps aggressively and alerts
   the owner (rate-limited, so a full disk does not spam).
3. ⏳ **States** - multi-step conversations (captcha, setup wizards) that were
   abandoned are dropped so their memory can be reclaimed.
4. 📡 **Connectivity** - pings Telegram; after 3 failures in a row the process
   restarts itself (``AUTO_RESTART_ON_HANG``), which is the only reliable cure
   for a wedged MTProto/Bot API session.
5. 🧠 **Memory** - trims registered caches and runs the garbage collector.

Everything is *reported*, not just logged: the log channel gets a message when
something was actually fixed.
"""

from __future__ import annotations

import asyncio
import gc
import os
import shutil
import sys
import time
from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..logging_setup import get_logger

log = get_logger("watchdog")

__all__ = ["CleanupStats", "Watchdog"]

#: ``(kind, user_id) -> (first_seen_ts, id(state))``; a new state object resets
#: the timer, so an active conversation is never expired.
SeenMap = dict[tuple[str, int], tuple[float, int]]


class CleanupStats:
    """Running totals, exposed through ``/watchdog`` and the status page."""

    def __init__(self) -> None:
        self.sweeps = 0
        self.files_removed = 0
        self.bytes_freed = 0
        self.states_dropped = 0
        self.disk_alerts = 0
        self.restarts = 0
        self.last: dict[str, Any] = {}

    def summary(self) -> dict[str, Any]:
        return {
            "sweeps": self.sweeps,
            "files_removed": self.files_removed,
            "freed_mb": round(self.bytes_freed / 1_048_576, 1),
            "states_dropped": self.states_dropped,
            "disk_alerts": self.disk_alerts,
            "last": self.last,
        }


def _tree_size(path: str) -> int:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _newest_mtime(path: str) -> float:
    if not os.path.isdir(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0
    newest = 0.0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                continue
    if newest:
        return newest
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def clean_dir(base: str, max_age_hours: float, *, keep: set[str] | None = None) -> tuple[int, int]:
    """Delete entries in ``base`` whose newest file is older than the limit.

    Returns ``(files_removed, bytes_freed)``. Errors are swallowed: a watchdog
    that raises is worse than one that skips a file.
    """
    keep = keep or set()
    removed = freed = 0
    if not base or not os.path.isdir(base):
        return 0, 0
    cutoff = time.time() - max_age_hours * 3600
    for name in os.listdir(base):
        if name in keep or name.startswith("."):
            continue
        path = os.path.join(base, name)
        try:
            if _newest_mtime(path) > cutoff:
                continue
            size = _tree_size(path)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed += 1
            freed += size
        except OSError as exc:
            log.debug("cleanup skipped %s: %s", path, exc)
    return removed, freed


class Watchdog:
    """Periodic maintenance sweep."""

    def __init__(
        self,
        settings: Settings,
        *,
        get_me: Callable[[], Any] | None = None,
        states: list[tuple[str, Callable[[], dict[int, Any]], Callable[[int], Any]]] | None = None,
        notify: Callable[[str, str], Any] | None = None,
        caches: list[Callable[[], int]] | None = None,
        on_restart: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.get_me = get_me
        self.states = states or []
        self.notify = notify
        self.caches = caches or []
        self.on_restart = on_restart
        self.stats = CleanupStats()
        self.seen: SeenMap = {}
        self.conn_failures = 0
        self.memory_mb = 0.0
        self._last_disk_alert = 0.0
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # -- public --------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._task is None and self.settings.watchdog:
            self._task = asyncio.get_running_loop().create_task(self.run_forever())
            log.info(
                "armed: sweep every %ss, cleanup after %sh",
                self.settings.watchdog_interval,
                self.settings.cleanup_after_hours,
            )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def run_forever(self) -> None:
        self._running = True
        await asyncio.sleep(60)  # let the bot finish booting
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let a sweep kill the loop
                log.exception("sweep failed: %s", exc)
            await asyncio.sleep(self.settings.watchdog_interval)

    async def sweep(self, aggressive: bool = False) -> dict[str, Any]:
        started = time.time()
        report: dict[str, Any] = {}

        free_gb = shutil.disk_usage(".").free / 1_073_741_824
        if free_gb < self.settings.min_free_disk_gb:
            aggressive = True

        report["files"], report["freed_mb"] = self.clean_files(aggressive)
        report["free_disk_gb"] = round(shutil.disk_usage(".").free / 1_073_741_824, 2)
        if report["free_disk_gb"] < self.settings.min_free_disk_gb:
            await self._alert_disk(report["free_disk_gb"])
        report["states_dropped"] = await self.expire_states()
        report["telegram_ok"] = await self.check_connection()
        report["ram_mb"] = self.trim_memory()
        report["took_s"] = round(time.time() - started, 2)
        report["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        report["aggressive"] = aggressive

        self.stats.sweeps += 1
        self.stats.last = report
        if report["files"] or report["states_dropped"]:
            log.info("sweep: %s", report)
        return report

    # -- 1 + 2: files --------------------------------------------------------

    def clean_files(self, aggressive: bool = False) -> tuple[int, int]:
        age = 0.5 if aggressive else self.settings.cleanup_after_hours
        files = freed = 0
        for directory in self.settings.temp_dir_list:
            count, size = clean_dir(str(directory), age)
            files += count
            freed += size
        self.stats.files_removed += files
        self.stats.bytes_freed += freed
        return files, round(freed / 1_048_576, 1)

    async def _alert_disk(self, free_gb: float) -> None:
        if self.notify is None:
            return
        if time.time() - self._last_disk_alert < 3 * 3600:
            return
        self._last_disk_alert = time.time()
        self.stats.disk_alerts += 1
        try:
            result = self.notify(
                "LowDisk",
                f"⚠️ <b>Low disk space:</b> <code>{free_gb:.2f} GB</code> free "
                f"(threshold {self.settings.min_free_disk_gb} GB).\n🧹 Aggressive cleanup ran.",
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log.warning("disk alert failed: %s", exc)

    # -- 3: abandoned conversations -----------------------------------------

    async def expire_states(self) -> int:
        timeout = self.settings.state_timeout_min * 60
        now = time.time()
        dropped = 0
        live: set[tuple[str, int]] = set()

        for kind, get_map, on_expire in self.states:
            try:
                mapping = get_map()
            except Exception:
                continue
            for user_id, state in list(mapping.items()):
                key = (kind, user_id)
                live.add(key)
                first_seen, ident = self.seen.get(key, (now, id(state)))
                if ident != id(state):
                    first_seen = now  # a fresh conversation restarts the clock
                self.seen[key] = (first_seen, id(state))
                if now - first_seen > timeout:
                    mapping.pop(user_id, None)
                    dropped += 1
                    try:
                        result = on_expire(user_id)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        log.debug("expire callback for %s/%s failed: %s", kind, user_id, exc)

        for key in [k for k in self.seen if k not in live]:
            self.seen.pop(key, None)
        self.stats.states_dropped += dropped
        return dropped

    # -- 4: connectivity -----------------------------------------------------

    async def check_connection(self) -> bool:
        if self.get_me is None:
            return True
        try:
            result = self.get_me()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=30)
            self.conn_failures = 0
            return True
        except Exception as exc:
            self.conn_failures += 1
            log.warning("Telegram check failed (%sx): %s", self.conn_failures, exc)
            if self.conn_failures >= 3 and self.settings.auto_restart_on_hang:
                await self.restart("Telegram unreachable 3 checks in a row")
            return False

    async def restart(self, reason: str) -> None:
        """Restart the process (the only cure for a wedged session)."""
        self.stats.restarts += 1
        log.error("restarting: %s", reason)
        if self.on_restart is not None:
            try:
                result = self.on_restart(reason)
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=20)
            except Exception as exc:
                log.warning("restart hook failed: %s", exc)
        try:  # best effort: give pending log sends a chance to land
            await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            pass
        os.execl(sys.executable, sys.executable, *sys.argv)

    # -- 5: memory -----------------------------------------------------------

    def trim_memory(self) -> float:
        for trim in self.caches:
            try:
                trim()
            except Exception as exc:
                log.debug("cache trim failed: %s", exc)
        gc.collect()
        self.memory_mb = _rss_mb()
        return self.memory_mb


def _rss_mb() -> float:
    """Resident memory in MB, without requiring psutil."""
    try:  # Linux
        with open("/proc/self/statm", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576, 1)
    except Exception:
        pass
    try:  # macOS / BSD
        import resource

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1_048_576, 1)
    except Exception:
        return 0.0
