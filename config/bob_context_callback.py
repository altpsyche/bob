"""LiteLLM callback shim beside `config/litellm.yaml`.

LiteLLM loads a configured callback relative to the config file's directory (`config_file_path`),
not relative to `sys.path`. The real implementation lives in `scripts/bob_context_callback.py`; this
shim makes the generated `- bob_context_callback.proxy_handler_instance` entry importable and gives a
hand-run `litellm --config config/litellm.yaml` the same callback as a Bob-managed stack start.
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bob_context_callback import proxy_handler_instance  # noqa: E402,F401
