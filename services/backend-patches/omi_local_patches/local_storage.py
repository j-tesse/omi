"""Fix for upstream's filesystem storage adapter (backend/utils/other/local_storage.py).

The sync bridge copies donor audio with
``bucket.copy_blob(src, bucket, new_path, if_generation_match=<0|generation>, retry=None)``
and, when a create-only copy loses a race, reads the winner with
``bucket.get_blob(new_path, retry=None)`` (backend/utils/conversations/merge_conversations.py).
Upstream's ``LocalBucket.copy_blob`` takes no keyword arguments and ``LocalBucket`` has
no ``get_blob``, so every offline sync that merges audio into an existing conversation
fails in finalize (``sync_finalize_exception exception_type=TypeError site=bridge_effects``)
and the app retries that batch forever.

Same change as the fork commit a552d4f (proposed upstream on
``fix/local-storage-bridge-copy-preconditions``): ``copy_blob`` applies the destination
precondition the way ``upload_from_string`` does (0 = create-only, otherwise the current
generation must match), and ``get_blob`` returns the loaded blob or ``None``.

The patch steps aside as soon as upstream ships its own ``LocalBucket.get_blob``.
"""

from __future__ import annotations

import logging
import shutil

log = logging.getLogger('omi_local_patches')

MARKER = '_omi_local_patched'


def apply(module) -> bool:
    """Patch ``module.LocalBucket``. Returns True if the patch was applied."""
    bucket_cls = getattr(module, 'LocalBucket', None)
    if bucket_cls is None or getattr(bucket_cls, MARKER, False):
        return False
    if 'get_blob' in vars(bucket_cls):
        log.warning('omi_local_patches: upstream LocalBucket already has get_blob; local_storage patch skipped')
        return False

    precondition_failed = module.PreconditionFailed

    def get_blob(self, blob_name: str, *_args, **_kwargs):
        blob = self.blob(blob_name)
        if not blob.exists():
            return None
        blob.reload()
        return blob

    def copy_blob(self, blob, destination_bucket, new_name: str, **kwargs):
        destination = destination_bucket.blob(new_name)
        match = kwargs.get('if_generation_match')
        if match not in (None, 0):
            destination._check_generation(int(match))
        destination.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with blob.path.open('rb') as source, destination.path.open('xb' if match == 0 else 'wb') as target:
                shutil.copyfileobj(source, target)
        except FileExistsError as error:
            raise precondition_failed('local storage create-only object exists') from error
        return destination

    bucket_cls.get_blob = get_blob
    bucket_cls.copy_blob = copy_blob
    setattr(bucket_cls, MARKER, True)
    log.info('omi_local_patches: patched LocalBucket.copy_blob/get_blob')
    return True
