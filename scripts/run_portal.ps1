# Start the synthetic TPA portal on http://127.0.0.1:8001 (single worker: state is in memory).
Set-Location $PSScriptRoot\..
& .\.venv\Scripts\python.exe -m uvicorn portal.app:app --host 127.0.0.1 --port 8001 --reload
