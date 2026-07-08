"""ONE-C Slice 5 — agent scheduling (scripts/tools/schedule.py + the osenv scheduler quartet + the
runner). Hermetic: the schedule store + log live in a tempdir, the agent run (bob_loop.run_agent) is
mocked, and every crontab/schtasks call is mocked, so nothing hits the network, an LLM, or the real OS
scheduler.

cron_due parity is asserted directly against the exact Test-CronDue semantics (D3): UTC, 5 fields,
'*'/comma/'a-b' ranges only, Sunday=0, and the 60-second re-fire guard."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import schedule as sched  # noqa: E402
import osenv  # noqa: E402


def _dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def _cfg(tmp):
    return {"agent": {"enabled": True, "scheduleFile": str(Path(tmp) / "sched.json"),
                      "logFile": str(Path(tmp) / "agent.log"), "maxResultChars": 100},
            "routing": {"agentRole": "agent"}}


class TestRegistryWiring(unittest.TestCase):
    VERBS = ["agent schedule", "agent log", "agent install", "agent uninstall", "agent status"]

    def test_flipped_to_python_with_handlers(self):
        by_name = registry.by_name()
        for verb in self.VERBS:
            self.assertTrue(by_name[verb].get("handler"), verb)
            self.assertIn(by_name[verb]["handler"], cli._HANDLERS, verb)

    def test_tools_and_mutation_flags(self):
        self.assertEqual(set(sched.DISPATCH), {
            "schedule_list", "schedule_add", "schedule_remove", "schedule_enable", "schedule_disable",
            "schedule_run", "agent_task_status", "agent_log"})
        self.assertEqual(sched.MUTATING_TOOLS, {
            "schedule_add", "schedule_remove", "schedule_enable", "schedule_disable", "schedule_run"})

    def test_install_uninstall_are_cli_only_not_tools(self):
        # install/uninstall register/remove the OS task -> CLI/--run only, never in-loop agent tools.
        self.assertNotIn("agent_install", sched.DISPATCH)
        self.assertNotIn("agent_uninstall", sched.DISPATCH)


class TestCronDue(unittest.TestCase):
    SUN = _dt(2026, 7, 5, 9, 0)   # a Sunday, 09:00 UTC
    MON = _dt(2026, 7, 6, 9, 0)   # a Monday, 09:00 UTC

    def test_every_minute(self):
        self.assertTrue(sched.cron_due("* * * * *", self.SUN))

    def test_sunday_is_zero(self):
        self.assertTrue(sched.cron_due("0 9 * * 0", self.SUN))
        self.assertFalse(sched.cron_due("0 9 * * 1-5", self.SUN))   # weekday range excludes Sunday

    def test_weekday_range_matches_monday(self):
        self.assertTrue(sched.cron_due("0 9 * * 1-5", self.MON))

    def test_comma_list(self):
        self.assertTrue(sched.cron_due("0,30 * * * *", _dt(2026, 7, 6, 10, 30)))
        self.assertFalse(sched.cron_due("0,30 * * * *", _dt(2026, 7, 6, 10, 15)))

    def test_minute_must_match(self):
        self.assertFalse(sched.cron_due("30 14 * * *", _dt(2026, 7, 6, 14, 0)))

    def test_bad_field_count(self):
        self.assertFalse(sched.cron_due("* * *", self.SUN))

    def test_sixty_second_guard(self):
        last = _dt(2026, 7, 5, 9, 0)
        self.assertFalse(sched.cron_due("* * * * *", last + timedelta(seconds=30), last))
        self.assertTrue(sched.cron_due("* * * * *", last + timedelta(seconds=90), last))

    def test_no_step_syntax(self):
        # '*/5' is NOT supported (exact port) — treated as a non-matching literal, never due.
        self.assertFalse(sched.cron_due("*/5 * * * *", _dt(2026, 7, 6, 10, 0)))


class TestCrud(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bob-sched-")
        self.cfg = _cfg(self.tmp)

    def _add(self, **kw):
        with mock.patch("osenv.agent_task_status", return_value={"registered": True, "state": "Ready", "next_run": None}):
            return sched.schedule_add(self.cfg, kw.pop("name", "digest"), **kw)

    def test_add_list_remove_roundtrip(self):
        self.assertIn("Added 'digest'", self._add(cron="0 9 * * 1-5", goal="summarize inbox"))
        out = sched.schedule_list(self.cfg)
        self.assertIn("digest", out)
        self.assertIn("0 9 * * 1-5", out)
        self.assertIn("Removed 'digest'", sched.schedule_remove(self.cfg, "digest"))
        self.assertIn("No schedules", sched.schedule_list(self.cfg))

    def test_duplicate_rejected(self):
        self._add()
        self.assertIn("already exists", self._add())

    def test_enable_disable(self):
        self._add()
        self.assertIn("Disabled: digest", sched.schedule_disable(self.cfg, "digest"))
        self.assertIn("Enabled: digest", sched.schedule_enable(self.cfg, "digest"))
        self.assertIn("Not found", sched.schedule_enable(self.cfg, "ghost"))

    def test_add_autoregisters_when_unregistered(self):
        with mock.patch("osenv.agent_task_status", return_value={"registered": False, "state": None, "next_run": None}), \
             mock.patch("osenv.venv_exe", return_value=Path("/venv/bin/python")), \
             mock.patch("osenv.register_agent_task") as reg:
            out = sched.schedule_add(self.cfg, "digest")
        reg.assert_called_once()
        self.assertIn("auto-registered", out)


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bob-sched-")
        self.cfg = _cfg(self.tmp)
        with mock.patch("osenv.agent_task_status", return_value={"registered": True, "state": "Ready", "next_run": None}):
            sched.schedule_add(self.cfg, "digest", cron="* * * * *", goal="do it")

    def test_run_now_persists_result(self):
        with mock.patch.object(sched, "_run_goal", return_value="the answer") as rg, \
             mock.patch("osenv.notify") as notify:
            out = sched.schedule_run(self.cfg, "digest")
        rg.assert_called_once()
        notify.assert_not_called()  # notify defaults false
        self.assertIn("the answer", out)
        self.assertIn("the answer", sched.schedule_list(self.cfg))

    def test_run_due_fires_only_when_due(self):
        with mock.patch.object(sched, "cron_due", return_value=True), \
             mock.patch.object(sched, "_run_goal", return_value="out"):
            self.assertIn("ran 1: digest", sched.run_due_schedules(self.cfg))
        with mock.patch.object(sched, "cron_due", return_value=False), \
             mock.patch.object(sched, "_run_goal", return_value="out") as rg:
            self.assertEqual(sched.run_due_schedules(self.cfg), "nothing due")
            rg.assert_not_called()

    def test_disabled_agent_short_circuits(self):
        cfg = dict(self.cfg)
        cfg["agent"] = dict(cfg["agent"], enabled=False)
        with mock.patch.object(sched, "_run_goal") as rg:
            self.assertIn("agent disabled", sched.run_due_schedules(cfg))
            rg.assert_not_called()

    def test_result_truncated_to_max_chars(self):
        long = "x" * 500
        with mock.patch.object(sched, "cron_due", return_value=True), \
             mock.patch.object(sched, "_run_goal", return_value=long):
            sched.run_due_schedules(self.cfg)
        entry = next(e for e in sched._read_schedules(self.cfg) if e["name"] == "digest")
        self.assertEqual(len(entry["lastRunResult"]), 100)  # maxResultChars


class TestOsenvQuartet(unittest.TestCase):
    def test_spec_linux(self):
        spec = osenv.agent_task_spec("/venv/bin/python", "/repo/scripts/bob_agent_runner.py", os="linux")
        self.assertEqual(spec["kind"], "cron")
        self.assertTrue(spec["crontab"].startswith("* * * * * "))
        self.assertTrue(spec["crontab"].endswith("# BobAgent"))

    def test_spec_windows(self):
        spec = osenv.agent_task_spec("C:/py.exe", "C:/runner.py", os="windows")
        self.assertEqual(spec["kind"], "schtasks")
        self.assertEqual(spec["command"], '"C:/py.exe" "C:/runner.py"')

    def _crontab(self, lines):
        def fake_run(argv, **kw):
            r = mock.Mock()
            if argv[:2] == ["crontab", "-l"]:
                r.returncode = 0
                r.stdout = "\n".join(lines) + ("\n" if lines else "")
            else:
                r.returncode = 0
                r.stdout = ""
            return r
        return fake_run

    def test_status_detects_tagged_line(self):
        tagged = '* * * * * /venv/bin/python /repo/scripts/bob_agent_runner.py # BobAgent'
        with mock.patch("shutil.which", return_value="/usr/bin/crontab"), \
             mock.patch("subprocess.run", side_effect=self._crontab([tagged, "0 5 * * * backup # other"])):
            st = osenv.agent_task_status()
        self.assertEqual(st, {"registered": True, "state": "Ready", "next_run": None})

    def test_status_no_crontab_binary(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(osenv.agent_task_status()["registered"], False)

    def test_register_is_idempotent(self):
        existing = ['* * * * * OLD /repo/scripts/bob_agent_runner.py # BobAgent', '0 5 * * * backup # other']
        written = {}

        def fake_run(argv, **kw):
            r = mock.Mock()
            if argv[:2] == ["crontab", "-l"]:
                r.returncode = 0
                r.stdout = "\n".join(existing) + "\n"
            elif argv == ["crontab", "-"]:
                written["payload"] = kw.get("input", "")
                r.returncode = 0
            else:
                r.returncode = 1
                r.stdout = ""
            return r
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(osenv, "os_name", return_value="linux"), \
             mock.patch("osenv.crontab_available", return_value=True), \
             mock.patch("subprocess.run", side_effect=fake_run):
            osenv.register_agent_task("/venv/bin/python", "/repo/scripts/bob_agent_runner.py")
        payload = written["payload"]
        # exactly one BobAgent line (the stale one replaced), the unrelated 'other' line preserved
        self.assertEqual(payload.count("# BobAgent"), 1)
        self.assertIn("# other", payload)
        self.assertNotIn("OLD", payload)


if __name__ == "__main__":
    unittest.main()
