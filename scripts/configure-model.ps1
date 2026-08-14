param(
    [string]$BaseUrl = "",
    [string]$ApiKey = "",
    [string]$ModelName = "qwen3.7-plus"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot ".env"

function Read-FuraRequiredValue {
    param(
        [string]$CurrentValue,
        [string]$Prompt
    )
    if (-not [string]::IsNullOrWhiteSpace($CurrentValue)) {
        return $CurrentValue.Trim()
    }
    return (Read-Host $Prompt).Trim()
}

try {
    Write-Host "Fura-AI model configuration" -ForegroundColor Cyan
    Write-Host "The API key is written only to the local .env file, which is excluded from Git."

    $BaseUrl = Read-FuraRequiredValue $BaseUrl "OpenAI-compatible base URL (for example https://host/v1)"
    $ApiKey = Read-FuraRequiredValue $ApiKey "API key"
    $ModelName = Read-FuraRequiredValue $ModelName "Model name"

    $parsedUrl = $null
    if (-not [Uri]::TryCreate($BaseUrl, [UriKind]::Absolute, [ref]$parsedUrl) -or $parsedUrl.Scheme -notin @("http", "https")) {
        throw "The model base URL must be an absolute HTTP(S) URL."
    }
    if ($ApiKey.Length -lt 8 -or $ApiKey -match "replace_with|your[_-]?key|[\r\n]") {
        throw "The API key is empty or still contains a placeholder."
    }
    if ($ModelName -notmatch "^[A-Za-z0-9._-]+$") {
        throw "The model name may contain only letters, numbers, dot, underscore and hyphen."
    }

    if (Test-Path -LiteralPath $envPath) {
        $backup = Join-Path $projectRoot (".env.backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
        Copy-Item -LiteralPath $envPath -Destination $backup
        Write-Host "Existing configuration backed up to: $backup"
    }

    $content = @(
        "# Local model configuration. Never commit this file.",
        "AGENT_MODEL_BASE_URL=$($BaseUrl.TrimEnd('/'))",
        "AGENT_MODEL_API_KEY=$ApiKey",
        "AGENT_MODEL_NAME=$ModelName",
        "AGENT_MODEL_THINKING=auto",
        "AGENT_VIDEO_MAX_CONCURRENCY=1"
    ) -join "`n"
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($envPath, $content + "`n", $utf8WithoutBom)
    Write-Host "Model configuration saved. You can now double-click 启动电脑端.bat." -ForegroundColor Green
    exit 0
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
