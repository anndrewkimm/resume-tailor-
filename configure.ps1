$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$examplePath = Join-Path $repoRoot "backend/.env.example"
$envPath = Join-Path $repoRoot "backend/.env"
$extensionConfigPath = Join-Path $repoRoot "extension/config.local.js"

function Get-DotEnvValue([string[]] $Lines, [string] $Key) {
    $match = $Lines | Where-Object { $_ -match "^\s*$([regex]::Escape($Key))\s*=" } | Select-Object -First 1
    if ($null -eq $match) { return "" }
    return ($match -split "=", 2)[1].Trim()
}

$exampleLines = [System.IO.File]::ReadAllLines($examplePath)
$existingLines = if (Test-Path $envPath) { [System.IO.File]::ReadAllLines($envPath) } else { @() }

$sharedSecret = Get-DotEnvValue $existingLines "SHARED_SECRET"
if (-not $sharedSecret -or $sharedSecret -eq "replace-with-a-long-random-secret") {
    $bytes = [byte[]]::new(32)
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    $sharedSecret = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

$ollamaHost = Get-DotEnvValue $existingLines "OLLAMA_HOST"
if (-not $ollamaHost) { $ollamaHost = Get-DotEnvValue $exampleLines "OLLAMA_HOST" }
if (-not $ollamaHost) { $ollamaHost = "http://127.0.0.1:11434" }
$ollamaModel = Get-DotEnvValue $existingLines "OLLAMA_MODEL"
if (-not $ollamaModel) { $ollamaModel = Get-DotEnvValue $exampleLines "OLLAMA_MODEL" }
if (-not $ollamaModel) { $ollamaModel = "qwen2.5:7b-instruct" }

$envLines = $exampleLines | ForEach-Object {
    if ($_ -match "^\s*SHARED_SECRET\s*=") { "SHARED_SECRET=$sharedSecret" }
    elseif ($_ -match "^\s*OLLAMA_HOST\s*=") { "OLLAMA_HOST=$ollamaHost" }
    elseif ($_ -match "^\s*OLLAMA_MODEL\s*=") { "OLLAMA_MODEL=$ollamaModel" }
    else { $_ }
}
$safeExampleLines = $exampleLines | ForEach-Object {
    if ($_ -match "^\s*SHARED_SECRET\s*=") { "SHARED_SECRET=replace-with-a-long-random-secret" }
    else { $_ }
}

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllLines($envPath, $envLines, $utf8NoBom)
[System.IO.File]::WriteAllLines($examplePath, $safeExampleLines, $utf8NoBom)
[System.IO.File]::WriteAllText(
    $extensionConfigPath,
    "globalThis.RESUME_TAILOR_LOCAL = Object.freeze({ sharedSecret: '$sharedSecret' });`n",
    $utf8NoBom
)

Write-Output "Local Ollama backend and extension credentials configured. Secret values were not printed."
