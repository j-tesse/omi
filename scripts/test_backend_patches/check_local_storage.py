"""Check the omi_local_patches local_storage fix against upstream's real LocalBucket.

Runs inside a backend-image container (cwd /app, PYTHONPATH=/opt/omi-local-patches),
so `utils.other.local_storage` is imported through the patch hook exactly as the
services import it. Replays the sync bridge's call shapes from
backend/utils/conversations/merge_conversations.py (_copy_bridge_chunk).
"""

import sys
import tempfile
from pathlib import Path

from google.api_core.exceptions import PreconditionFailed

import utils.other.local_storage as ls

failures = 0


def check(label, ok, detail=''):
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {label}{' - ' + detail if detail else ''}")
    failures += 0 if ok else 1


patched = getattr(ls.LocalBucket, '_omi_local_patched', False)
native = 'get_blob' in vars(ls.LocalBucket) and not patched
print(f'hook installed: {"omi_local_patches" in sys.modules}; patched: {patched}; upstream-native get_blob: {native}')
check('LocalBucket has get_blob (patched or upstream)', hasattr(ls.LocalBucket, 'get_blob'))

with tempfile.TemporaryDirectory() as tmp:
    bucket = ls.LocalBucket(Path(tmp), 'sync-temporal')
    src = bucket.blob('uid/donor/chunk_000.bin')
    src.upload_from_string(b'donor-audio')
    target = 'uid/target/chunk_000.bin'

    # 1. create-only copy (no current object): generation 0
    try:
        bucket.copy_blob(src, bucket, target, if_generation_match=0, retry=None)
        check('create-only copy (if_generation_match=0, retry=None)', bucket.blob(target).path.read_bytes() == b'donor-audio')
    except Exception as e:  # noqa: BLE001
        check('create-only copy (if_generation_match=0, retry=None)', False, repr(e))

    # 2. second create-only copy loses the race -> PreconditionFailed, then get_blob(winner)
    try:
        bucket.copy_blob(src, bucket, target, if_generation_match=0, retry=None)
        check('create-only copy onto existing object raises PreconditionFailed', False, 'no error')
    except PreconditionFailed:
        check('create-only copy onto existing object raises PreconditionFailed', True)
    except Exception as e:  # noqa: BLE001
        check('create-only copy onto existing object raises PreconditionFailed', False, repr(e))
    winner = bucket.get_blob(target, retry=None)
    check('get_blob returns the winner with size/crc32c',
          winner is not None and winner.size == src.size and winner.crc32c == src.crc32c)
    check('get_blob on a missing object returns None', bucket.get_blob('uid/missing.bin', retry=None) is None)

    # 3. replace exactly the listed generation; a stale generation is refused
    current = bucket.get_blob(target)
    src2 = bucket.blob('uid/donor/chunk_001.bin')
    src2.upload_from_string(b'newer-audio')
    try:
        bucket.copy_blob(src2, bucket, target, if_generation_match=current.generation + 1, retry=None)
        check('stale generation raises PreconditionFailed', False, 'no error')
    except PreconditionFailed:
        check('stale generation raises PreconditionFailed', True)
    bucket.copy_blob(src2, bucket, target, if_generation_match=current.generation, retry=None)
    check('matching generation replaces the object', bucket.blob(target).path.read_bytes() == b'newer-audio')

    # 4. unconditional copy (non-bridge callers) still works
    bucket.copy_blob(src, bucket, 'uid/plain/chunk.bin')
    check('unconditional copy', bucket.blob('uid/plain/chunk.bin').path.read_bytes() == b'donor-audio')

print('OK' if failures == 0 else f'{failures} failure(s)')
sys.exit(1 if failures else 0)
