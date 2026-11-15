"""Async worker pool.

* Tickets are enqueued by id; the queue never holds state, the database does.
* A semaphore caps concurrent SDK subprocesses (each stage spawns the Claude Code CLI).
* On startup, every non-terminal ticket that isn't waiting on a human is re-enqueued.
  The controller's pending-call recovery decides whether it's safe to continue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .contracts import Phase
from .controller import TicketController

log = logging.getLogger(__name__)

RESUMABLE = [Phase.RECEIVED, Phase.TRIAGING, Phase.RESOLVING, Phase.REPLYING]


class WorkerPool:
    def __init__(self, controller: TicketController, concurrency: int) -> None:
        self.controller = controller
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.concurrency = concurrency
        self._tasks: list[asyncio.Task[None]] = []

    def enqueue(self, ticket_id: str) -> None:
        self.queue.put_nowait(ticket_id)

    def recover(self) -> int:
        ids = self.controller.store.tickets_in_phases([p.value for p in RESUMABLE])
        for tid in ids:
            self.enqueue(tid)
        if ids:
            log.info("re-enqueued %d unfinished ticket(s)", len(ids))
        return len(ids)

    async def _loop(self, n: int) -> None:
        while True:
            ticket_id = await self.queue.get()
            try:
                state = await self.controller.advance(ticket_id)
                log.info(
                    "ticket %s → %s (%s)",
                    ticket_id,
                    state.phase.value,
                    state.stop_reason.value if state.stop_reason else "-",
                )
            except Exception:
                log.exception("worker %d failed on %s", n, ticket_id)
            finally:
                self.queue.task_done()

    def start(self) -> None:
        self.recover()
        self._tasks = [asyncio.create_task(self._loop(i)) for i in range(self.concurrency)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
