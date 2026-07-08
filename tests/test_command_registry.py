"""The command registry is THE single source for dispatch + help. Proves every entry is well-formed and
maps to a real cli.py handler, and that no generated verb table or PowerShell scaffolding remains
(registry.COMMANDS is the only catalog — nothing to keep in sync). This is also the one place that pins
every verb to a live handler, so the per-domain test files don't each re-assert their own wiring."""
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry


class TestRegistry(unittest.TestCase):
    def test_enumerable_and_well_formed(self):
        cmds = registry.commands()
        self.assertTrue(cmds)
        for c in cmds:
            for field in ("name", "group", "summary", "args", "handler"):
                self.assertIn(field, c, f"{c.get('name')} missing {field}")
            self.assertNotIn("runtime", c, f"{c['name']} still carries the retired runtime field")
            self.assertIn(c["handler"], cli._HANDLERS, f"{c['name']} handler not wired")

    def test_every_verb_maps_to_a_handler(self):
        # The load-bearing verbs across every domain map to a live handler. test_enumerable_and_well_formed
        # already proves this for the WHOLE catalog; this list makes the per-domain coverage explicit in one
        # place (lifecycle, models, provisioning, build, health, scheduling, memory/meta) so the domain test
        # files can drop their own redundant wiring checks.
        by = registry.by_name()
        for name in (
            # entry / server / misc
            "agent serve", "agent mcp", "clip", "help",
            # lifecycle
            "serve", "stop", "up", "restart", "status", "ps", "logs", "webui",
            "litellm", "whisper", "piper", "services",
            # health
            "setup", "doctor", "diagnose", "version",
            # models + eval
            "models", "show", "profiles", "profile", "verify-urls", "bench", "eval",
            # build / update / provisioning
            "build", "fabric-setup", "update", "fetch", "lock", "mlock", "setup-voice",
            # config generation
            "gen",
            # memory + meta
            "remember", "recall", "memory", "budget", "tools", "plugins", "fabric", "aider",
            # conversation
            "chat", "code", "think",
            # scheduling
            "agent schedule", "agent log", "agent install", "agent uninstall", "agent status",
        ):
            self.assertIn(name, by, name)
            self.assertIn(by[name]["handler"], cli._HANDLERS, name)

    def test_no_verbs_json_machinery(self):
        # The verb table was collapsed: the generated file + its generator/sync gate are gone.
        self.assertFalse((Path(registry.REPO) / "config" / "verbs.json").exists())
        for gone in ("verbs_json_dict", "write_verbs", "_check", "VERBS_FILE"):
            self.assertFalse(hasattr(registry, gone), f"registry.{gone} should be removed")


class TestNoPowerShellFrontDoor(unittest.TestCase):
    """The PowerShell front door (bob.ps1) + seam library are retired; registry.COMMANDS is the sole
    catalog."""

    def test_help_is_registry_driven(self):
        self.assertEqual(registry.by_name()["help"]["handler"], "help")

    def test_no_pwsh_scripts_under_scripts(self):
        self.assertEqual(list((Path(registry.REPO) / "scripts").glob("*.ps1")), [])


class TestGroupOrderSingleSource(unittest.TestCase):
    """GROUP_ORDER is authored once in the registry; the plain catalog, the rich views, and the CLI
    help fallback all reference that same object (no copy to drift)."""

    def test_group_order_single_sourced(self):
        from bob import catalog, render
        self.assertIs(catalog._GROUP_ORDER, registry.GROUP_ORDER)
        self.assertIs(render._GROUP_ORDER, registry.GROUP_ORDER)
        self.assertIs(cli._GROUP_ORDER, registry.GROUP_ORDER)

    def test_every_command_group_is_known(self):
        for c in registry.commands():
            self.assertIn(c["group"], registry.GROUP_ORDER, c["name"])

    def test_catalog_renders_new_buckets_not_config(self):
        from bob import catalog
        out = catalog.render_commands()
        for g in ("Run", "Services", "Models", "Diagnose", "Setup"):
            self.assertIn(f"{g}:", out)
        self.assertNotIn("Config:", out)   # the old 17-verb wall was split


class TestSharedSurfaceSignpost(unittest.TestCase):
    """`bob help` names the commands that exist on BOTH surfaces, computed from the registry ∩ the
    shell's slash set so it can't drift into a hand-maintained list."""

    def test_shared_matches_actual_overlap(self):
        from bob.shell import slash_names
        verbs = {c["name"].split()[0] for c in registry.commands(include_hidden=True)}
        self.assertEqual(cli._shared_with_shell(), sorted(slash_names() & verbs))

    def test_shared_includes_cockpit_commands(self):
        shared = cli._shared_with_shell()
        for name in ("up", "stop", "restart", "status", "services", "webui", "logs"):
            self.assertIn(name, shared)

    def test_print_help_names_shared_commands(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            cli._print_help()
        out = buf.getvalue()
        self.assertIn("Also live in the shell", out)
        self.assertIn("/up", out)


class TestHandlerFlags(unittest.TestCase):
    """The overlap-cleanup flags: `bob doctor --quick` == `bob setup check` (one core), and `bob up`
    accepts both the POSIX and the legacy flag spellings."""

    def _patch(self, **attrs):
        saved = {k: getattr(cli, k) for k in attrs}
        for k, v in attrs.items():
            setattr(cli, k, v)
        self.addCleanup(lambda: [setattr(cli, k, v) for k, v in saved.items()])

    def test_doctor_quick_equals_setup_check(self):
        calls = []

        class _Health:
            def health_check(self, cfg, doctor=False):
                calls.append(doctor)
                return "health"

        self._patch(_health_mod=lambda: _Health(), _cfg=lambda: {})
        cli._handle_doctor([])             # full pre-flight
        cli._handle_doctor(["--quick"])    # fast health check
        cli._handle_setup(["check"])       # the back-compat alias
        self.assertEqual(calls, [True, False, False])

    def test_setup_rejects_unknown_subcommand(self):
        self._patch(_health_mod=lambda: None, _cfg=lambda: {})
        self.assertEqual(cli._handle_setup(["bogus"]), 1)

    def test_up_accepts_posix_and_legacy_flags(self):
        seen = []

        class _Stack:
            def stack_up(self, cfg, open_browser=True, with_services=False):
                seen.append((open_browser, with_services))
                return "up"

        self._patch(_stack=lambda: _Stack(), _cfg=lambda: {})
        cli._handle_up([])
        cli._handle_up(["--no-open", "--with-services"])
        cli._handle_up(["-NoOpen", "-WithServices"])
        self.assertEqual(seen, [(True, False), (False, True), (False, True)])


class TestResolve(unittest.TestCase):
    def test_two_token_command_wins(self):
        self.assertEqual(cli._resolve(["agent", "serve"]), ("agent serve", []))
        self.assertEqual(cli._resolve(["agent", "mcp", "x"]), ("agent mcp", ["x"]))

    def test_bare_verb_with_trailing_args(self):
        # 'agent <goal>' — the goal is not a subcommand, so it stays the bare 'agent'
        self.assertEqual(cli._resolve(["agent", "fix", "the", "bug"]),
                         ("agent", ["fix", "the", "bug"]))

    def test_single_verb(self):
        self.assertEqual(cli._resolve(["status"]), ("status", []))

    def test_empty(self):
        self.assertEqual(cli._resolve([]), (None, []))


if __name__ == "__main__":
    unittest.main()
