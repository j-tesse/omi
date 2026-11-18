"""Runtime fixes for upstream Omi, applied from outside the submodule.

Each entry in PATCHES maps an upstream module name to a function that patches the
module right after it is imported (see sitecustomize.py). Keep every patch small,
documented with the upstream bug, and self-disabling once upstream fixes it.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import logging
import sys

from . import local_storage, v3_action_items_lane, v3_local_queue, v3_participants

log = logging.getLogger('omi_local_patches')

PATCHES = {
    'utils.other.local_storage': local_storage.apply,
    'utils.llm.model_config': v3_action_items_lane.apply,
    'utils.conversations.v3.triggers': v3_local_queue.apply,
    'utils.conversations.v3.planner': v3_participants.apply_planner,
    'utils.conversations.v3.service': v3_participants.apply_service,
}


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, wrapped, patch):
        self._wrapped = wrapped
        self._patch = patch

    def create_module(self, spec):
        return self._wrapped.create_module(spec)

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        try:
            self._patch(module)
        except Exception:  # never take the service down because of a patch
            log.exception('omi_local_patches: patch for %s failed', module.__name__)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


class _PatchFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        patch = PATCHES.get(fullname)
        if patch is None:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, 'find_spec'):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, 'exec_module'):
                    spec.loader = _PatchingLoader(spec.loader, patch)
                return spec
        return None


def install() -> None:
    if any(isinstance(finder, _PatchFinder) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _PatchFinder())
    # Modules imported before install() (unlikely from sitecustomize) are patched directly.
    for name, patch in PATCHES.items():
        if name in sys.modules:
            patch(sys.modules[name])
