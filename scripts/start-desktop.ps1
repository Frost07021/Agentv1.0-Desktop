param(
    [switch]$SkipLaunch,
    [ValidateRange(1024, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvRoot = Join-Path $projectRoot ".venv"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$requirementsStamp = Join-Path $venvRoot ".requirements.sha256"
$runtimeDir = Join-Path $projectRoot ".runtime"
$stdoutLog = Join-Path $runtimeDir "server.log"
$stderrLog = Join-Path $runtimeDir "server-error.log"
$appUrl = "http://127.0.0.1:$Port"
$healthUrl = "$appUrl/health/ready"
$expectedBuildVersion = "desktop-1.4.34"

function Get-FuraSystemPython {
    $candidates = [System.Collections.Generic.List[string]]::new()

    try {
        foreach ($line in (& py.exe -0p 2>$null)) {
            if ($line -match '([A-Za-z]:\\.*python\.exe)\s*$') {
                $candidates.Add($Matches[1].Trim())
            }
        }
    }
    catch {
        # The Python launcher is optional.
    }

    foreach ($commandName in @("python.exe", "python3.exe")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($command -and $command.Source) {
            $candidates.Add($command.Source)
        }
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate)) {
            continue
        }
        try {
            $version = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version -match '^(\d+)\.(\d+)$') {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -eq 3 -and $minor -ge 12 -and $minor -lt 15) {
                    return $candidate
                }
            }
        }
        catch {
            # Try the next candidate.
        }
    }

    return $null
}

function Initialize-FuraPython {
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

    if (-not (Test-Path -LiteralPath $pythonPath)) {
        $systemPython = Get-FuraSystemPython
        if (-not $systemPython) {
            throw "Python 3.12-3.14 was not found. Install Python from https://www.python.org/downloads/ and run this launcher again."
        }
        Write-Host "[FURA] First launch: creating the local Python environment..."
        & $systemPython -m venv $venvRoot
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath)) {
            throw "Could not create the Python environment with: $systemPython"
        }
    }

    if (-not (Test-Path -LiteralPath $requirementsPath)) {
        throw "Dependency file was not found: $requirementsPath"
    }

    $requiredHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash
    $installedHash = if (Test-Path -LiteralPath $requirementsStamp) {
        (Get-Content -LiteralPath $requirementsStamp -Raw).Trim()
    }
    else {
        ""
    }

    $importsReady = $false
    if ($installedHash -eq $requiredHash) {
        & $pythonPath -c "import fastapi, uvicorn, httpx, pydantic, yaml, PIL, jsonschema, multipart, imageio_ffmpeg, pypdfium2" 2>$null
        $importsReady = $LASTEXITCODE -eq 0
    }

    if (-not $importsReady) {
        Write-Host "[FURA] Installing desktop dependencies. The first launch may take a few minutes..."
        & $pythonPath -m pip install --disable-pip-version-check -r $requirementsPath
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed. Check the network connection and try again."
        }
        Set-Content -LiteralPath $requirementsStamp -Value $requiredHash -Encoding ASCII
    }
}

function Get-FuraHealth {
    try {
        return Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    }
    catch {
        return $null
    }
}

function Test-FuraReady {
    $health = Get-FuraHealth
    return $null -ne $health `
        -and $health.status -eq "ready" `
        -and $health.build_version -eq $expectedBuildVersion `
        -and $health.skills -eq 7 `
        -and $health.routes -eq 6
}

function Get-FuraPortOwner {
    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($connection -and $connection.OwningProcess) {
            return [int]$connection.OwningProcess
        }
    }
    catch {
        # Fall back to netstat when Get-NetTCPConnection is unavailable.
    }
    foreach ($line in (netstat.exe -ano -p TCP)) {
        if ($line -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            return [int]$Matches[1]
        }
    }
    return $null
}

function Stop-StaleFuraServer {
    $health = Get-FuraHealth
    $looksLikeFura = $null -ne $health -and $health.build_version -like "desktop-*" -and $health.routes -eq 6
    if (-not $looksLikeFura -or (Test-FuraReady)) {
        return
    }
    $ownerPid = Get-FuraPortOwner
    if ($ownerPid) {
        Write-Host "[FURA] Replacing stale or incomplete desktop service ($($health.build_version))..."
        Stop-Process -Id $ownerPid -Force -ErrorAction Stop
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            Start-Sleep -Milliseconds 250
            if (-not (Get-FuraPortOwner)) {
                break
            }
        }
    }
}

try {
    Initialize-FuraPython

    $localEnv = Join-Path $projectRoot ".env"
    $externalPointer = Join-Path $projectRoot ".runtime\model-config.path"
    $processModelConfigured = -not [string]::IsNullOrWhiteSpace($env:AGENT_MODEL_BASE_URL) `
        -and -not [string]::IsNullOrWhiteSpace($env:AGENT_MODEL_API_KEY) `
        -and -not [string]::IsNullOrWhiteSpace($env:AGENT_MODEL_NAME)
    if (-not $processModelConfigured -and -not (Test-Path -LiteralPath $localEnv) -and -not (Test-Path -LiteralPath $externalPointer)) {
        Write-Host "[FURA] Real model is not configured. Run 配置模型.bat before using real analysis." -ForegroundColor Yellow
        Write-Host "[FURA] The interface and Fake mode can still be used."
    }

    if (-not (Test-FuraReady)) {
        Stop-StaleFuraServer
        Write-Host "[FURA] Starting desktop service..."
        Start-Process `
            -FilePath $pythonPath `
            -ArgumentList @("-m", "app", "serve", "--host", "127.0.0.1", "--port", "$Port") `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog | Out-Null

        $ready = $false
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            Start-Sleep -Milliseconds 500
            if (Test-FuraReady) {
                $ready = $true
                break
            }
        }
        if (-not $ready) {
            throw "Service did not become ready in 20 seconds. See: $stderrLog"
        }
    }

    if (-not $SkipLaunch) {
        $browsers = @(
            "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            "C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            "C:\Program Files\Google\Chrome\Application\chrome.exe"
        )
        $browser = $browsers | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if ($browser) {
            Start-Process -FilePath $browser -ArgumentList @("--app=$appUrl", "--new-window", "--start-maximized")
        }
        else {
            Start-Process $appUrl
        }
        Write-Host "[FURA] Desktop window opened."
    }
    else {
        Write-Host "[FURA] Startup check passed: $appUrl"
    }
    exit 0
}
catch {
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $message = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $($_.Exception.Message)"
    Add-Content -LiteralPath $stderrLog -Value $message -Encoding UTF8
    Write-Host "[FURA] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
