"""Generated-config validity gate: for every real profile in config/models.json, render every client
generator IN MEMORY (generate._write is patched to capture, so config/ is never touched) and parse each
YAML output. A generator change that emits unparsable YAML for any profile fails here, not on a user's
`bob gen`."""
import contextlib
import io
import json
import unittest
from unittest import mock

import _common  # noqa: F401 — puts scripts/ + scripts/tools/ on sys.path

try:
    import yaml
except ModuleNotFoundError as _e:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML not installed: {_e}")

import bob_core  # noqa: E402
import bob_models  # noqa: E402
import generate as gen  # noqa: E402


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that accepts application tags (dsh's patch format allows `!!js/...`) as plain nodes, so
    the gate checks YAML structure without executing or rejecting a tag the target app defines."""


def _any_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TolerantLoader.add_multi_constructor("!", _any_tag)
_TolerantLoader.add_multi_constructor("tag:", _any_tag)

_GENERATORS = ("gen_llama_swap", "gen_litellm", "gen_continue", "gen_dsh", "gen_aider")


def _render(profile: str) -> dict:
    """{relative path: text} for every file the generators would write for `profile`."""
    captured = {}

    def _capture(path, text):
        captured[str(path.relative_to(gen.REPO)).replace("\\", "/")] = text
        return path

    saved = gen._cfg
    gen.configure(bob_core.load_config())
    # stderr carries the generators' advisory notes (unset peer keys, absent group members): not under test.
    with mock.patch.object(gen, "_write", side_effect=_capture), contextlib.redirect_stderr(io.StringIO()):
        try:
            for fn in _GENERATORS:
                getattr(gen, fn)(profile)
        finally:
            gen._cfg = saved
    return captured


class TestGeneratedConfigsParse(unittest.TestCase):
    def test_every_profile_renders_parseable_yaml(self):
        profiles = [p for p in bob_models.load_models_config().get("profiles", {}) if not p.startswith("_")]
        self.assertTrue(profiles, "no profiles in config/models.json")
        for profile in profiles:
            with self.subTest(profile=profile):
                files = _render(profile)
                for want in ("config/llama-swap.yaml", "config/litellm.yaml",
                             "config/continue/config.yaml", "config/dsh/settings.yaml",
                             "config/aider/.aider.conf.yml"):
                    self.assertIn(want, files, f"{profile}: generator wrote no {want}")
                for rel, text in files.items():
                    if rel.endswith(".json"):
                        json.loads(text)
                        continue
                    if not rel.endswith((".yaml", ".yml")):
                        continue
                    try:
                        yaml.load(text, Loader=_TolerantLoader)   # noqa: S506 — SafeLoader subclass
                    except yaml.YAMLError as e:
                        self.fail(f"{profile}: {rel} is not valid YAML: {e}")

    def test_render_never_writes_config(self):
        with mock.patch.object(gen.Path, "write_text", side_effect=AssertionError("wrote to disk")):
            _render(_common.TEST_PROFILE)


if __name__ == "__main__":
    unittest.main()
