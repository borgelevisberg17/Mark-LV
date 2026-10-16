# Configure the Microsoft Graph public-client ID for this local checkout.
# Run from the Mark-LV repository root:
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup_microsoft_graph.ps1
$ErrorActionPreference = "Stop"

$repoRoot = (Get-Location).Path
$configRelativePath = "config/microsoft_graph.json"
$configPath = Join-Path $repoRoot $configRelativePath

if (-not (Test-Path (Join-Path $repoRoot ".git"))) {
    throw "Run this script from the root of a Git checkout of Mark-LV."
}

$clientId = Read-Host "Microsoft Entra Application (client) ID"
$tenantChoice = Read-Host "Account audience: common (personal + work/school), consumers (personal), organizations (work/school) [common]"
$tenantChoice = $tenantChoice.Trim().ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($tenantChoice)) { $tenantChoice = "common" }
if ($tenantChoice -notin @("common", "consumers", "organizations")) {
    throw "Tenant must be common, consumers, or organizations. No file was written."
}
$clientId = $clientId.Trim()
$parsedGuid = [Guid]::Empty
if (-not [Guid]::TryParse($clientId, [ref]$parsedGuid)) {
    throw "The client ID must be a valid GUID. No file was written."
}

# Fail closed if Git would track the personal config file.
$null = & git -C $repoRoot check-ignore -q $configRelativePath
if ($LASTEXITCODE -ne 0) {
    throw "$configRelativePath is not ignored by Git. Add it to .gitignore before continuing. No file was written."
}

$configDirectory = Split-Path -Parent $configPath
if (-not (Test-Path $configDirectory)) {
    New-Item -ItemType Directory -Path $configDirectory -Force | Out-Null
}

$json = @{ client_id = $parsedGuid.ToString(); tenant_id = $tenantChoice } | ConvertTo-Json
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($configPath, ($json + [Environment]::NewLine), $utf8WithoutBom)

Write-Host ""
Write-Host "Local Microsoft Graph configuration created."
Write-Host "Path: $configPath"
Write-Host "Git ignore check: passed (the file will not be committed by normal Git adds)."
Write-Host "Tenant audience: $tenantChoice"
Write-Host "No client secret or token was created or stored."
Write-Host ""
Write-Host "Next: ensure Python dependencies are installed, launch Mark-LV, then call office_graph with operation=start_login."
Write-Host "Complete the device-code sign-in in your browser, then call office_graph with operation=poll_login."
