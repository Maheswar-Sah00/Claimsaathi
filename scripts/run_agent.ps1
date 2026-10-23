# Start the ClaimSaathi agent on http://127.0.0.1:8000.
Set-Location $PSScriptRoot\..
& .\.venv\Scripts\python.exe -m uvicorn agent.server:app --host 127.0.0.1 --port 8000 --reload
