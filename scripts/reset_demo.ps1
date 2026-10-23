# Reset the ClaimSaathi demo: restore the synthetic seed in the agent database, clear checkpoints,
# test results and evidence/RUN-*. Restart the portal (or use its /admin "Reset demo data") to reset it too:
# the portal keeps its state in memory.
Set-Location $PSScriptRoot\..
& .\.venv\Scripts\python.exe -m agent.cli reset-demo --yes
