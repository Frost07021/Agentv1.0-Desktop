param(
    [switch]$AllowGitDirectory
)

$ErrorActionPreference = "Stop"
$projectRoot = (Split-Path -Parent $PSScriptRoot)
$requiredFiles = @(
    ".env.example",
    ".gitattributes",
    ".gitignore",
    ".github\workflows\tests.yml",
    "README.md",
    "QUICKSTART.md",
    "requirements.txt",
    "pyproject.toml",
    "启动电脑端.bat",
    "配置模型.bat",
    "app\api.py",
    "config\home-check-plugins.yaml",
    "skill-definitions\ProjectF-home check-gait\SKILL.md",
    "static\index.html"
)

foreach ($relativePath in $requiredFiles) {
    $path = Join-Path $projectRoot $relativePath
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required package file is missing: $relativePath"
    }
}

$forbidden = @(
    ".env",
    ".runtime",
    ".venv",
    ".pytest_cache",
    "runtime",
    "output",
    "项目优化过程与最新验收报告.md"
)
foreach ($relativePath in $forbidden) {
    if (Test-Path -LiteralPath (Join-Path $projectRoot $relativePath)) {
        throw "Private or generated path must not be in the GitHub package: $relativePath"
    }
}
if (-not $AllowGitDirectory -and (Test-Path -LiteralPath (Join-Path $projectRoot ".git"))) {
    throw "The distributable archive must not contain a .git directory."
}

$cacheFiles = Get-ChildItem -LiteralPath $projectRoot -Recurse -File -Force | Where-Object {
    $_.Name -like "*.larkcache" -or $_.Name -like "*.pyc" -or $_.FullName -match "[\\/]__pycache__[\\/]"
}
if ($cacheFiles) {
    throw "Generated cache files are present in the package: $($cacheFiles[0].FullName)"
}

$skillCount = (Get-ChildItem -LiteralPath (Join-Path $projectRoot "skill-definitions") -Recurse -File -Filter "SKILL.md").Count
if ($skillCount -ne 7) {
    throw "Expected 7 Skill definitions, found $skillCount."
}

$sensitivePatterns = @(
    ("C:" + "\\Users\\Administrator"),
    ("D:" + "\\MaxoutModels"),
    ("(?<![A-Za-z0-9])s" + "k-[A-Za-z0-9_-]{20,}")
)
$textFiles = Get-ChildItem -LiteralPath $projectRoot -Recurse -File -Force | Where-Object {
    $_.Extension -in @(".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".js", ".css", ".html", ".ps1", ".bat", ".example")
}
foreach ($file in $textFiles) {
    $text = [IO.File]::ReadAllText($file.FullName)
    foreach ($pattern in $sensitivePatterns) {
        if ($text -match $pattern) {
            throw "Sensitive or machine-specific content found in $($file.FullName): $pattern"
        }
    }
}

Write-Host "Package verification passed: required files, 7 Skills, privacy boundaries and cache exclusions are valid."
