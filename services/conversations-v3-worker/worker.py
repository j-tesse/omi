"""Local stand-in for upstream's conversations-v3 Cloud Tasks handlers.

Upstream builds v3 conversations (outline, title, summary, memories) from synced
recordings in two Cloud Tasks routes (backend/routers/conversations_v3_tasks.py):
- ``/v1/conversations-v3/reconcile`` per uid, scheduled by ``triggers.enqueue``.
- ``/v1/conversations-v3/sweep``, a periodic safety net that re-enqueues every uid with
  overdue ``v3_pending`` / ``v3_process_pending`` markers and retries audio purges.

Here ``triggers.enqueue`` writes to a Redis sorted set (omi_local_patches.v3_local_queue,
needs ``OMI_LOCAL_V3_QUEUE=redis``). This loop runs due uids through the same upstream
``service.reconcile`` and calls ``service.sweep`` every ``V3_SWEEP_SECONDS``. A failed or
busy reconcile is retried after ``V3_RETRY_SECONDS``. Runs in the backend image with the
backend env, one reconcile at a time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, '/app')

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(levelname)s %(name)s: %(message)s')
log = logging.getLogger('conversations_v3_worker')

POLL_SECONDS = float(os.getenv('V3_POLL_SECONDS', '5'))
SWEEP_SECONDS = float(os.getenv('V3_SWEEP_SECONDS', '300'))
RETRY_SECONDS = float(os.getenv('V3_RETRY_SECONDS', '60'))
HEARTBEAT_FILE = os.getenv('V3_HEARTBEAT_FILE', '/tmp/conversations-v3-worker.heartbeat')


def _init_firebase() -> None:
    """Same emulator branch as backend/main.py (database.auth needs a default app)."""
    import firebase_admin

    if firebase_admin._apps:
        return
    project = os.getenv('FIREBASE_AUTH_PROJECT_ID') or os.getenv('FIREBASE_PROJECT_ID') or 'demo-omi-local'
    firebase_admin.initialize_app(options={'projectId': project})


def _beat() -> None:
    with open(HEARTBEAT_FILE, 'w') as fh:
        fh.write(str(int(time.time())))


async def main() -> int:
    if os.getenv('OMI_LOCAL_V3_QUEUE', '').strip().lower() != 'redis':
        log.error('OMI_LOCAL_V3_QUEUE=redis is required (the triggers.enqueue patch feeds this worker)')
        return 2
    _init_firebase()
    from database.redis_db import r
    from omi_local_patches.v3_local_queue import QUEUE_KEY, schedule
    from utils.conversations.v3 import service, triggers
    from utils.executors import run_blocking, db_executor

    if not getattr(triggers.enqueue, '_omi_local_patched', False):
        log.error('triggers.enqueue is not patched; check PYTHONPATH=/opt/omi-local-patches')
        return 2

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info('started queue=%s poll=%ss sweep=%ss retry=%ss', QUEUE_KEY, POLL_SECONDS, SWEEP_SECONDS, RETRY_SECONDS)
    next_sweep = 0.0
    while not stop.is_set():
        _beat()
        now = time.time()
        if now >= next_sweep:
            next_sweep = now + SWEEP_SECONDS
            try:
                log.info('sweep enqueued=%s', await service.sweep())
            except Exception:
                log.exception('sweep failed')
        due = await run_blocking(db_executor, r.zrangebyscore, QUEUE_KEY, '-inf', now, 0, 1)
        if not due:
            try:
                await asyncio.wait_for(stop.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            continue
        uid = due[0].decode() if isinstance(due[0], bytes) else due[0]
        # Claim it; reconcile may schedule a new due time for the same uid while it runs.
        await run_blocking(db_executor, r.zrem, QUEUE_KEY, uid)
        started = time.monotonic()
        try:
            outcome = await service.reconcile(uid)
        except Exception:
            log.exception('reconcile failed uid=%s; retry in %ss', uid, RETRY_SECONDS)
            outcome = 'error'
        if outcome in ('busy', 'error'):
            await run_blocking(
                db_executor, schedule, uid, datetime.now(timezone.utc) + timedelta(seconds=RETRY_SECONDS)
            )
        log.info('reconcile uid=%s outcome=%s seconds=%.1f', uid, outcome, time.monotonic() - started)
    log.info('stopped')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
