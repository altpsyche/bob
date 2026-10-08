"""LiteLLM pre-call hook for Bob's Quick/Deep context modes.

This is the single enforcement seam for every OpenAI-compatible client that talks to Bob's LiteLLM
proxy.  It does no budget math of its own: the model-name suffix selects a mode, then the shared
``bob_context.apply_openai_request`` seam applies the same policy Bob's own agent loop uses.
"""
from __future__ import annotations

import logging
from typing import Any

try:  # imported only inside the LiteLLM proxy process
    from litellm.integrations.custom_logger import CustomLogger
except Exception:  # pragma: no cover - LiteLLM is a runtime dep of this module
    CustomLogger = object  # type: ignore

from bob_context import apply_openai_request

log = logging.getLogger("bob.context")


class BobContextCallback(CustomLogger):
    """LiteLLM CustomLogger that applies Bob's mode policy before the provider call."""

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: Any,
                                  call_type: str) -> Any:
        if not isinstance(data, dict):
            return data
        # Only chat-completion-shaped requests carry messages and a model name.
        if not data.get("messages") or not isinstance(data.get("model"), str):
            return data
        try:
            from bob_core import load_config
            return apply_openai_request(load_config(), data["model"], data)
        except Exception as e:  # noqa: BLE001 - a mode hook must never break the proxy
            log.warning("context mode hook failed for model %s: %s", data.get("model"), e)
            return data


proxy_handler_instance = BobContextCallback()
