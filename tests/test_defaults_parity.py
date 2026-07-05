"""NB1 — config/defaults.json is the neutral single source of truth for ports + roles, read
identically by Python (bob_core.load_defaults / _port / get_role) and PowerShell (_models.ps1
Get-BobPortDefault / Get-RoleForTask). This proves the two sides agree and that a dropped key
fails loudly on the Python side."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
import bob_config
import bob_core

REPO = Path(bob_core.REPO)
DEFAULTS = REPO / "config" / "defaults.json"

# A fully-populated routing/vision config so both languages resolve to the routing *values*
# (the fallback literals only matter for sparse configs, which production never has).
_CFG = {
    "routing": {
        "defaultRole": "chat", "proRole": "chat-pro",
        "codeRole": "coder", "proCodeRole": "coder-pro",
        "thinkRole": "planner", "proThinkRole": "planner-pro",
        "agentRole": "agent",
    },
    "vision": {"visionRole": "vision", "visionProRole": "vision-pro"},
}
# Tasks the PowerShell Get-RoleForTask ValidateSet accepts (no 'agent' there).
_TASKS = ["chat", "code", "think", "vision", "voice"]


class TestDefaultsPythonSide(unittest.TestCase):
    def test_ports_load_and_resolve(self):
        ports = bob_core.load_defaults()["ports"]
        for name in ("port", "litellmPort", "agentPort", "searxngPort", "sttPort",
                     "ttsPort", "webuiPort", "langfusePort", "n8nPort"):
            self.assertIn(name, ports)
            self.assertEqual(bob_core._port({}, name), ports[name])

    def test_role_table_drives_get_role(self):
        for task in _TASKS:
            self.assertEqual(bob_core.get_role(_CFG, task),
                             bob_core.get_role(_CFG, task, pro=False))

    def test_missing_ports_section_raises_clearly(self):
        bad = Path(tempfile.mkdtemp(prefix="bob-def-")) / "defaults.json"
        bad.write_text(json.dumps({"roleTable": {}}), encoding="utf-8")
        orig_file, orig_cache = bob_core._DEFAULTS_FILE, bob_core._defaults_cache
        try:
            bob_core._DEFAULTS_FILE = bad
            bob_core._defaults_cache = None
            with self.assertRaises(RuntimeError) as ctx:
                bob_core.load_defaults()
            self.assertIn("ports", str(ctx.exception))
        finally:
            bob_core._DEFAULTS_FILE, bob_core._defaults_cache = orig_file, orig_cache
            shutil.rmtree(bad.parent, ignore_errors=True)


@unittest.skipUnless(shutil.which("pwsh"), "pwsh not available — Python/PowerShell parity skipped")
class TestDefaultsParityWithPowerShell(unittest.TestCase):
    """Prove PowerShell reads config/defaults.json to the same values Python does."""

    def _pwsh(self, script: str) -> str:
        r = subprocess.run(["pwsh", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, cwd=str(REPO), timeout=90)
        self.assertEqual(r.returncode, 0, f"pwsh failed:\n{r.stdout}\n{r.stderr}")
        return r.stdout.strip()

    def test_ports_identical(self):
        out = self._pwsh(". ./scripts/_models.ps1; "
                         "$script:BobPortDefaults | ConvertTo-Json -Compress")
        ps_ports = {k: int(v) for k, v in json.loads(out).items()}
        self.assertEqual(ps_ports, bob_core.load_defaults()["ports"])

    def test_no_shadow_port_literals_in_psd1(self):
        """WI-5 backstop: config/defaults.json.ports is the SOLE port source. No port key may be
        re-introduced in models.psd1.defaults or anywhere in bob.psd1 (including the voice block)."""
        script = r"""
$ports = @('port','litellmPort','webuiPort','langfusePort','searxngPort','n8nPort','sttPort','ttsPort','agentPort')
$m = Import-PowerShellDataFile config/models.psd1
$b = Import-PowerShellDataFile config/bob.psd1
$bad = [System.Collections.Generic.List[string]]::new()
foreach ($k in @($m.defaults.Keys)) { if ($ports -contains $k) { $bad.Add("models.defaults.$k") } }
function Find-Ports($h, $prefix, $acc, $ports) {
  foreach ($k in @($h.Keys)) {
    if ($ports -contains $k) { $acc.Add("$prefix$k") }
    if ($h[$k] -is [hashtable]) { Find-Ports $h[$k] "$prefix$k." $acc $ports }
  }
}
Find-Ports $b 'bob.' $bad $ports
$bad -join ','
"""
        out = self._pwsh(script)
        self.assertEqual(out, "", f"shadow port literal(s) reintroduced — single-source them in "
                                  f"config/defaults.json.ports and read via Get-BobPortDefault: {out}")

    def test_roles_identical(self):
        cfg_ps = ("@{routing=@{defaultRole='chat';proRole='chat-pro';codeRole='coder';"
                  "proCodeRole='coder-pro';thinkRole='planner';proThinkRole='planner-pro';"
                  "agentRole='agent'};vision=@{visionRole='vision';visionProRole='vision-pro'}}")
        lines = []
        for t in _TASKS:
            lines.append(f'"{t}|" + (Get-RoleForTask -Config $c -Task {t})')
            lines.append(f'"{t}-pro|" + (Get-RoleForTask -Config $c -Task {t} -Pro)')
        script = f". ./scripts/_models.ps1; $c={cfg_ps}; " + "; ".join(lines)
        ps = dict(line.split("|", 1) for line in self._pwsh(script).splitlines())
        for t in _TASKS:
            self.assertEqual(ps[t], bob_core.get_role(_CFG, t), f"role mismatch: {t}")
            self.assertEqual(ps[f"{t}-pro"], bob_core.get_role(_CFG, t, pro=True),
                             f"pro role mismatch: {t}")


    def test_runtime_layer_parity_with_python_resolver(self):
        """NB7 (Option A) acceptance: Windows Get-BobConfig now seeds the runtime layer from
        config/defaults.json.runtime (+ psd1 overlay), so the runtime keys it produces must match
        the Python resolve_runtime_config() — the "identical runtime config" guarantee."""
        out = self._pwsh(". ./scripts/_models.ps1; Get-BobConfig | ConvertTo-Json -Depth 10")
        ps = json.loads(out)
        py = bob_config.resolve_runtime_config()

        # Python emits the runtime SUBSET; Windows is a superset (adds voice, persona.name/style,
        # routing.autoFallback, agent.toastAppId, extra port injects). Assert Python's keys ⊆ Windows's
        # and that every shared runtime key resolves to the same value.
        def assert_subset(expected, actual, path):
            self.assertIsInstance(actual, dict, f"{path}: PS side is not a dict")
            for k, v in expected.items():
                self.assertIn(k, actual, f"{path}.{k} present in Python resolver but missing on Windows")
                if isinstance(v, dict):
                    assert_subset(v, actual[k], f"{path}.{k}")
                elif isinstance(v, list):
                    # arrays default empty; PowerShell's ConvertTo-Json unwraps single-element arrays,
                    # so compare as sets of stringified members (order/scalar-vs-array agnostic).
                    a = actual[k] if isinstance(actual[k], list) else [actual[k]]
                    self.assertEqual({str(x) for x in v}, {str(x) for x in a}, f"{path}.{k} mismatch")
                else:
                    self.assertEqual(v, actual[k], f"{path}.{k} mismatch (py={v!r} ps={actual[k]!r})")

        assert_subset(py, ps, "cfg")

    def test_persona_from_neutral_layer(self):
        """ONE-A: persona.name/style were deleted (dead keys). persona now resolves entirely from
        config/defaults.json.runtime.persona — Get-BobConfig must still carry systemPrompt from there."""
        out = self._pwsh(". ./scripts/_models.ps1; (Get-BobConfig).persona | ConvertTo-Json -Compress")
        persona = json.loads(out)
        self.assertTrue(persona.get("systemPrompt"), "systemPrompt missing — neutral persona not seeded")

    def test_agent_section_deep_merges_not_shallow(self):
        """WI-6 regression guard (retargeted after ONE-A removed the persona overlay): the agent.* runtime
        keys come from defaults.json.runtime.agent while agent.toastAppId comes from the bob.psd1 overlay.
        A SHALLOW merge would drop one side — the merged agent must carry BOTH the overlay's toastAppId
        AND a neutral key (maxSteps)."""
        out = self._pwsh(". ./scripts/_models.ps1; (Get-BobConfig).agent | ConvertTo-Json -Compress")
        agent = json.loads(out)
        self.assertTrue(agent.get("toastAppId"), "toastAppId dropped — overlay lost in a shallow merge")
        self.assertIn("maxSteps", agent, "maxSteps dropped — neutral runtime lost in a shallow merge")

    def test_routing_derived_not_authored(self):
        """ONE-A Finding 2: routing is derived from the roleTable (Get-DefaultRouting), not authored in
        bob.psd1. Get-BobConfig.routing must match the Python resolver's derived routing exactly."""
        out = self._pwsh(". ./scripts/_models.ps1; (Get-BobConfig).routing | ConvertTo-Json -Compress")
        ps_routing = json.loads(out)
        py_routing = bob_config.resolve_runtime_config()["routing"]
        self.assertEqual(ps_routing, py_routing, "routing drift between PS Get-DefaultRouting and Python")

    def test_litellm_key_from_neutral_not_models_psd1(self):
        """ONE-A Finding 1: litellmKey resolves from defaults.json.runtime.litellmKey on both sides —
        not re-sourced from models.psd1. Setting runtime.litellmKey must be honored by Get-BobConfig."""
        out = self._pwsh(". ./scripts/_models.ps1; (Get-BobConfig).litellmKey")
        self.assertEqual(out, bob_core.load_defaults()["runtime"]["litellmKey"],
                         "litellmKey not sourced from defaults.json.runtime")


if __name__ == "__main__":
    unittest.main()
