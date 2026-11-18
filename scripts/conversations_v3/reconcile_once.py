"""Run upstream's conversations-v3 reconcile for one or more uids, once.

Upstream builds v3 conversations from synced recordings in a Cloud Tasks
handler (POST /v1/conversations-v3/reconcile). There is no Cloud Tasks locally,
so this calls the same service function directly. Its follow-up enqueues
(continuations for unfinished stretches, retries) are replaced with a log line:
rerun this script, or let the local worker loop handle them.

Usage (inside the backend image, see scripts/conversations_v3_reconcile.sh):
    python reconcile_once.py <uid> [<uid> ...]
"""

import asyncio
import logging
import sys

sys.path.insert(0, '/app')

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
log = logging.getLogger('reconcile_once')


def _deferred(uid, *, at=None):
    log.info('enqueue skipped (no Cloud Tasks) uid=%s at=%s', uid, at)


async def main(uids):
    from utils.conversations.v3 import service, triggers

    service.enqueue = _deferred
    triggers.enqueue = _deferred
    failed = False
    for uid in uids:
        log.info('pending before uid=%s: %s', uid, service.has_pending(uid))
        try:
            outcome = await service.reconcile(uid)
        except Exception:
            log.exception('reconcile failed uid=%s', uid)
            failed = True
            continue
        log.info('reconcile uid=%s outcome=%s pending after=%s', uid, outcome, service.has_pending(uid))
    return 1 if failed else 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(sys.argv[1:])))
