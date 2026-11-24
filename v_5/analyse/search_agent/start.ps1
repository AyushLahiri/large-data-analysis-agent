[CmdletBinding()]
param([switch]$BuildOnly)
$ErrorActionPreference = 'Stop'
$env:OPENAI_API_KEY = if ($env:OPENAI_API_KEY) { $env:OPENAI_API_KEY } else {
    $file = if ($env:V5_KEYS_FILE) { $env:V5_KEYS_FILE } else { Join-Path $PSScriptRoot '..\..\..\keys.txt' }
    $line = Get-Content -LiteralPath $file | Where-Object { $_ -match '^\s*OPENAI_API_KEY\s*=' } | Select-Object -First 1
    if (!$line) { throw 'Missing OPENAI_API_KEY in the environment or v5 keys file.' }
    ($line -split '=',2)[1].Trim()
}
if (!$env:V5_ANALYSIS_SEARCH_TOKEN) {
    $dir = Join-Path $env:LOCALAPPDATA 'AtlasV5\Search'
    [IO.Directory]::CreateDirectory($dir) | Out-Null
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    icacls $dir /inheritance:r /grant:r ("*" + $sid + ':(OI)(CI)F') | Out-Null
    if ($LASTEXITCODE) { throw 'Could not secure search credential directory.' }
    $path = Join-Path $dir 'analysis.token'
    if (!(Test-Path -LiteralPath $path)) {
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        $bytes = New-Object byte[] 48
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $temp = Join-Path $dir ([guid]::NewGuid().ToString('N'))
        [IO.File]::WriteAllText($temp,[Convert]::ToBase64String($bytes))
        try { [IO.File]::Move($temp,$path) } catch {
            if (!(Test-Path -LiteralPath $path)) { throw }
            [IO.File]::Delete($temp)
        }
    }
    $env:V5_ANALYSIS_SEARCH_TOKEN = [IO.File]::ReadAllText($path).Trim()
}
Push-Location $PSScriptRoot
try {
    if ($BuildOnly) { docker compose build } else { docker compose up -d --build }
    if ($LASTEXITCODE) { throw 'Search service deployment failed.' }
} finally { Pop-Location }

