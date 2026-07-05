# Bob persona and behavior configuration — the Windows OVERLAY on config/defaults.json.runtime.
# Override any key in config/user.psd1 under a 'bob' section.
#
# NB7 (Option A): the runtime defaults (persona.systemPrompt, memory, vision, voice, agent.*) live in
# the neutral config/defaults.json 'runtime' layer — the single source shared with the Python resolver.
# Get-BobConfig seeds from there and deep-merges this file, so this psd1 carries ONLY the one key that
# is genuinely Windows-specific: agent.toastAppId.
#
# ONE-A (single-source config): the 'persona' block (name/style — dead keys, read nowhere), the
# 'routing' block (values duplicated the roleTable), and the 'voice' block (settings now in
# defaults.json.runtime.voice; its voice-only systemPrompt is dead — /voice uses the shared persona and
# strips markdown via bob_voice.format_for_speech) were all DELETED here. persona resolves from
# defaults.json.runtime.persona; routing is derived from the roleTable by _models.ps1 Get-DefaultRouting
# (mirroring Python bob_config._routing_from_role_table). Do NOT re-add persona/routing/voice/memory/
# vision or other agent keys — change them in config/defaults.json (both OSes) or in config/user.psd1.
@{
  agent = @{
    # All agent runtime keys (enabled, agency, maxSteps, timeouts, paths, ports, tokens, …) live in
    # config/defaults.json.runtime.agent. Only toastAppId is Windows-specific and stays here.
    toastAppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\powershell.exe'
  }
}
