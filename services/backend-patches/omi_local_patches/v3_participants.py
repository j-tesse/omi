"""Fix for upstream's conversations-v3 participants (backend/utils/conversations/v3/).

``planner.build`` (added in 012329830a, "Home redesign: recordings and conversations (v3)")
turns the outline's people into ``structured.participants`` as ``{'name': name}``.
``Structured.Participant.source`` (backend/models/structured.py) is a required
``Literal['roster', 'transcript']``, so as soon as the outline names anyone the v3
summary step (``service._process`` → ``Conversation(**row)``) raises a pydantic
``ValidationError`` ("structured.participants.0.source Field required"). The reconcile
aborts in ``drain_processing`` and every conversation of that user stays without a
summary.

The outline only ever sees the transcript, so the missing value is ``'transcript'``.
Two wrappers fill it in and leave set values alone:
- ``planner.build``: rows written from now on are valid.
- ``service._process``: rows already stored with the bad shape can still be summarized.
Each steps aside once upstream's ``planner.build`` sets a participant source itself.
"""

from __future__ import annotations

import functools
import inspect
import logging

log = logging.getLogger('omi_local_patches')

MARKER = '_omi_local_patched'
_UPSTREAM_FIX_HINTS = ("'source': 'transcript'", "'source': 'roster'", '"source": "transcript"')


def fill_source(row):
    participants = ((row or {}).get('structured') or {}).get('participants')
    for participant in participants or []:
        if isinstance(participant, dict) and not participant.get('source'):
            participant['source'] = 'transcript'
    return row


def _upstream_fixed() -> bool:
    import sys

    planner = sys.modules.get('utils.conversations.v3.planner')
    build = getattr(planner, 'build', None)
    build = getattr(build, '__wrapped__', build)
    try:
        text = inspect.getsource(build)
    except (OSError, TypeError):
        return False
    return any(hint in text for hint in _UPSTREAM_FIX_HINTS)


def apply_planner(module) -> bool:
    """Wrap ``planner.build``. Returns True if the patch was applied."""
    build = getattr(module, 'build', None)
    if build is None or getattr(build, MARKER, False):
        return False
    if _upstream_fixed():
        log.warning('omi_local_patches: upstream planner.build sets participant source; v3 planner patch skipped')
        return False

    @functools.wraps(build)
    def patched_build(*args, **kwargs):
        return fill_source(build(*args, **kwargs))

    setattr(patched_build, MARKER, True)
    module.build = patched_build
    log.info('omi_local_patches: patched v3 planner.build participant source')
    return True


def apply_service(module) -> bool:
    """Wrap ``service._process(uid, row, token)``. Returns True if the patch was applied."""
    process = getattr(module, '_process', None)
    if process is None or getattr(process, MARKER, False):
        return False
    if _upstream_fixed():  # service imports planner first, so it is loaded (and checked) by now
        log.warning('omi_local_patches: upstream planner.build sets participant source; v3 service patch skipped')
        return False

    @functools.wraps(process)
    def patched_process(uid, row, token, *args, **kwargs):
        return process(uid, fill_source(row), token, *args, **kwargs)

    setattr(patched_process, MARKER, True)
    module._process = patched_process
    log.info('omi_local_patches: patched v3 service._process participant source')
    return True
