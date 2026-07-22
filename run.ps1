# Start the Efforti outreach engine on Windows (PowerShell).
# First run creates outreach.db and seeds the default sequence.
# Usage:  ./run.ps1     then open http://localhost:8000
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Load .env into the process environment
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $idx = $line.IndexOf("=")
            $key = $line.Substring(0, $idx).Trim()
            $val = $line.Substring($idx + 1).Trim()
            [Environment]::SetEnvironmentVariable($key, $val, "Process")
        }
    }
}

& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
