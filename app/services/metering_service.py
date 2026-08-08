"""Per-tenant cost metering — bounded Firestore writes via in-process accumulation.

``record()`` adds to an in-process buffer keyed by ``(tenant, period, meter)``.
A periodic flush coalesces the buffer into ONE Firestore write per meter per
flush interval (an atomic increment), not one write per image.  The flush runs
on the interval, on lifespan shutdown, and on SIGTERM.  The residual loss
window if the process is killed between flushes is one flush interval —
documented here and not claimed to be zero.

This is the opposite of the audit service: metering is best-effort by design
(losing one interval of cost data is acceptable; losing an audit event is not),
so the shutdown drain swallows a store failure rather than blocking process exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime

from app.billing.meters import METER_UNITS, Meter
from app.repositories.usage_repo import UsageStore

logger = logging.getLogger("vurarad.metering")


def period_for(at: datetime) -> str:
    """Return the ``YYYY-MM`` billing period for ``at`` (in UTC)."""
    return at.astimezone(UTC).strftime("%Y-%m")


class MeteringService:
    """Accumulates metered quantities in-process and flushes them periodically.

    The bounded-write invariant: across any flush, the number of Firestore
    writes equals the number of distinct ``(tenant, period, meter)`` tuples that
    accumulated a non-zero delta — never the number of ``record()`` calls.
    """

    def __init__(self, store: UsageStore, *, flush_interval_seconds: float) -> None:
        self._store = store
        self._flush_interval = flush_interval_seconds
        self._buffer: dict[tuple[str, str, Meter], float] = {}
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def record(
        self,
        tenant_id: str,
        meter: Meter,
        value: float,
        *,
        ref: str = "",
        at: datetime | None = None,
    ) -> None:
        """Accumulate ``value`` of ``meter`` for ``tenant_id`` — no Firestore write."""
        del ref  # reserved for future event correlation; not persisted per-record
        period = period_for(at or datetime.now(UTC))
        key = (tenant_id, period, meter)
        async with self._lock:
            self._buffer[key] = self._buffer.get(key, 0.0) + value

    async def flush(self) -> None:
        """Drain the buffer: one Firestore write per accumulated meter.

        Raises whatever the store raises — callers that want best-effort
        behaviour (the shutdown drain) wrap this themselves.
        """
        async with self._lock:
            if not self._buffer:
                return
            pending = self._buffer
            self._buffer = {}
        for (tenant_id, period, meter), delta in pending.items():
            await self._store.increment_meter(
                tenant_id, period, meter, delta, METER_UNITS[meter]
            )

    async def start(self) -> None:
        """Begin the periodic flush loop and install the SIGTERM drain."""
        self._stopping = False
        self._flush_task = asyncio.create_task(self._run())
        self._install_sigterm()

    async def _run(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._flush_interval)
            await self.flush()

    def _install_sigterm(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows / no event-loop signal support — the lifespan shutdown
            # drain still runs; only the out-of-band SIGTERM catch is missing.
            logger.debug("SIGTERM handler not installed on this platform")

    def _on_sigterm(self) -> None:
        logger.info("SIGTERM received — scheduling metering drain")
        asyncio.create_task(self.stop())

    async def stop(self) -> None:
        """Cancel the periodic loop and perform a best-effort final drain."""
        self._stopping = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        # Best-effort drain: metering may lose at most one flush interval
        # (documented). A drain failure must not prevent process exit.
        try:
            await self.flush()
        except Exception:  # noqa: BLE001 - intentional best-effort, see module docstring
            logger.exception("metering drain failed on shutdown")
