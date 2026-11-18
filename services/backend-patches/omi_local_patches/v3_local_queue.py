"""Local queue for upstream's conversations-v3 builder (backend/utils/conversations/v3/triggers.py).

Upstream turns synced recordings into v3 conversations (title, summary, …) in a Cloud
Tasks handler: ``triggers.enqueue(uid, at=...)`` schedules a named task that POSTs
``/v1/conversations-v3/reconcile``. Without Cloud Tasks ``enqueue`` raises
``conversations_v3_task_target_unconfigured`` (logged as "v3 dispatch deferred
exception_type=RuntimeError"). The recording keeps ``v3_pending`` and never shows up in
the app.

With ``OMI_LOCAL_V3_QUEUE=redis`` this replaces ``triggers.enqueue`` with a Redis sorted
set ``local:conversations_v3:due`` (member uid, score = due unix time, earliest wins).
The ``conversations-v3-worker`` service (scripts/conversations_v3/worker.py) runs due
uids through upstream's ``service.reconcile``, the same function the Cloud Tasks route
calls. ``service`` imports ``enqueue`` by name, so this must patch ``triggers`` before
``service`` loads, which the import hook guarantees.

Skipped when the flag is unset or when ``CONVERSATIONS_V3_TASKS_URL`` is configured.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

log = logging.getLogger('omi_local_patches')

MARKER = '_omi_local_patched'
QUEUE_KEY = 'local:conversations_v3:due'


def schedule(uid: str, at: datetime | None = None) -> None:
    from database.redis_db import r

    instant = at or datetime.now(timezone.utc)
    # LT: an earlier due time replaces a later one; new members are always added.
    r.zadd(QUEUE_KEY, {uid: instant.timestamp()}, lt=True)


def apply(module) -> bool:
    enqueue = getattr(module, 'enqueue', None)
    if enqueue is None or getattr(enqueue, MARKER, False):
        return False
    if os.getenv('OMI_LOCAL_V3_QUEUE', '').strip().lower() != 'redis':
        return False
    if os.getenv('CONVERSATIONS_V3_TASKS_URL', '').strip():
        log.warning('omi_local_patches: CONVERSATIONS_V3_TASKS_URL set; v3 local queue patch skipped')
        return False

    def local_enqueue(uid, *, at=None):
        schedule(uid, at)

    setattr(local_enqueue, MARKER, True)
    local_enqueue.__wrapped__ = enqueue
    module.enqueue = local_enqueue
    log.info('omi_local_patches: patched v3 triggers.enqueue -> redis %s', QUEUE_KEY)
    return True
