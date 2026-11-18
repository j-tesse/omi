"""Fix for the gateway lane used by upstream's conversations-v3 short tasks.

``utils/conversations/v3/models.py`` ``short_tasks`` (added in 012329830a, "Home redesign:
recordings and conversations (v3)") summarizes every v3 conversation under 300 words by
calling lane ``omi:auto:conv-action-items`` with ``response_format: {type: json_object}``.
The gateway builds generated lanes with ``structured_output: json_schema`` only for
features in ``utils.llm.model_config._STRUCTURED_OUTPUT_FEATURES``
(``llm_gateway/gateway/config_loader.py`` ``_capabilities_for_feature``), and
``conv_action_items`` isn't in that set. So the lane gets ``structured_output: none``,
the gateway rejects the call with HTTP 400 "lane does not support structured output",
``process_conversation`` fails, and the v3 reconcile errors for every short recording.

This adds ``conv_action_items`` to the set. Apart from the gateway, the set is only read
for Gemini routing, which isn't used. Applies in the llm-gateway container (loaded at
gateway startup, so restart it after changing this). Steps aside once upstream lists the
feature itself.
"""

from __future__ import annotations

import logging

log = logging.getLogger('omi_local_patches')

FEATURE = 'conv_action_items'


def apply(module) -> bool:
    features = getattr(module, '_STRUCTURED_OUTPUT_FEATURES', None)
    if not isinstance(features, set):
        log.warning('omi_local_patches: model_config._STRUCTURED_OUTPUT_FEATURES missing; v3 action-items lane patch skipped')
        return False
    if FEATURE in features:
        return False
    features.add(FEATURE)  # STRUCTURED_OUTPUT_FEATURES is the same set object
    log.info('omi_local_patches: added %s to structured-output features (v3 short tasks)', FEATURE)
    return True
