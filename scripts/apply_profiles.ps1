# Apply tiered overclock profiles to MSI Afterburner config.
#
# Copies the VF curve from Startup to all 5 profile slots and sets per-profile
# memory clock offsets and fan speeds.
#
# Compatible with NVIDIA GTX 10-series (Pascal) and newer.
#
# Usage:
#     .\apply_profiles.ps1                  # Apply and copy
#     .\apply_profiles.ps1 -DryRun          # Preview without writing
#     .\apply_profiles.ps1 -ConfigPath "C:\path\to\custom.cfg"

param(
    [string]$ConfigPath,
    [switch]$DryRun
)

# ── Constants ────────────────────────────────────────────────────────────────
$AB_PROFILES_DIR = "C:\Program Files (x86)\MSI Afterburner\Profiles"

# Tiered profiles: name, mem_kHz, fan_%
$PROFILES = @(
    @{ Name="Profile1"; Mem=500000;  Fan=50 },
    @{ Name="Profile2"; Mem=600000;  Fan=55 },
    @{ Name="Profile3"; Mem=700000;  Fan=60 },
    @{ Name="Profile4"; Mem=800000;  Fan=70 },
    @{ Name="Profile5"; Mem=900000;  Fan=80 }
)

function TS { return (Get-Date -Format "[yyyy-MM-dd HH:mm:ss]") }

function Find-Config {
    if ($ConfigPath -and (Test-Path $ConfigPath)) { return $ConfigPath }
    if ($ConfigPath) { Write-Host "$(TS) ERROR: Config not found: $ConfigPath"; exit 1 }

    $matches = Get-ChildItem "$AB_PROFILES_DIR\VEN_10DE*.cfg" -ErrorAction SilentlyContinue
    if (-not $matches) {
        Write-Host "$(TS) ERROR: No NVIDIA config found in $AB_PROFILES_DIR"
        exit 1
    }
    $best = $matches | Sort-Object Length -Descending | Select-Object -First 1
    if ($matches.Count -gt 1) {
        Write-Host "$(TS) Found $($matches.Count) GPU configs, using largest: $($best.Name)"
    }
    return $best.FullName
}

function Set-IniValue {
    param([string]$block, [string]$key, [string]$value)
    $pattern = "(?m)^$([regex]::Escape($key))=.*$"
    if ($block -match $pattern) {
        return $block -replace $pattern, "$key=$value"
    } else {
        # Add after Format= if present, else append
        if ($block -match "(?m)^Format=.*$") {
            return $block -replace "(Format=.*)", "`$1`r`n$key=$value"
        }
        return $block.TrimEnd("`r`n") + "`r`n$key=$value`r`n"
    }
}

function Get-IniValue {
    param([string]$block, [string]$key)
    if ($block -match "(?m)^$([regex]::Escape($key))=(.+)$") {
        return $Matches[1].Trim()
    }
    return $null
}

# ── Main ─────────────────────────────────────────────────────────────────────
$cfgPath = Find-Config
Write-Host "$(TS) Config: $(Split-Path $cfgPath -Leaf)"
Write-Host "$(TS) Full path: $cfgPath"
Write-Host ""

if ($DryRun) {
    Write-Host "$(TS) DRY RUN — no files will be modified"
    Write-Host ""
}

# Read config as raw bytes to preserve encoding
$content = [System.IO.File]::ReadAllText($cfgPath, [System.Text.Encoding]::ASCII)

# Extract Startup VF curve
$startupMatch = [regex]::Match($content, '\[Startup\]([\s\S]*?)(?=\[(?:Profile\d|PreSuspendedMode|Defaults|Settings)\]|\z)')
if (-not $startupMatch.Success) {
    Write-Host "$(TS) ERROR: Cannot find [Startup] section"; exit 1
}
$startupBlock = $startupMatch.Groups[1].Value

$vfMatch = [regex]::Match($startupBlock, 'VFCurve=([0-9A-Fa-f]+)')
if (-not $vfMatch.Success) {
    Write-Host "$(TS) ERROR: No VFCurve in [Startup]. Set up your undervolt in Afterburner first."
    exit 1
}
$vfHex = $vfMatch.Groups[1].Value
$startupCore = if ($startupBlock -match "CoreClkBoost=(-?\d+)") { $Matches[1] } else { "0" }

Write-Host "$(TS) Source VF curve from [Startup]: $($vfHex.Length) hex chars"
Write-Host "$(TS) Startup CoreClkBoost: $([int]$startupCore / 1000) MHz"
Write-Host ""

# Apply to each profile
foreach ($prof in $PROFILES) {
    $pName = $prof.Name
    $memKhz = $prof.Mem
    $fanPct = $prof.Fan

    $secPattern = [regex]::Escape("[$pName]")
    $secMatch = [regex]::Match($content, "$secPattern([\s\S]*?)(?=\[(?:Startup|Profile\d|PreSuspendedMode|Defaults|Settings)\]|\z)")

    if ($secMatch.Success) {
        $block = $secMatch.Groups[1].Value
        $newBlock = Set-IniValue $block "CoreClkBoost" $startupCore
        $newBlock = Set-IniValue $newBlock "MemClkBoost" $memKhz
        $newBlock = Set-IniValue $newBlock "FanMode" "1"
        $newBlock = Set-IniValue $newBlock "FanSpeed" $fanPct
        $newBlock = Set-IniValue $newBlock "VFCurve" $vfHex
        if (-not (Get-IniValue $newBlock "PowerLimit")) {
            $newBlock = Set-IniValue $newBlock "PowerLimit" "100"
        }
        if (-not (Get-IniValue $newBlock "ThermalLimit")) {
            $newBlock = Set-IniValue $newBlock "ThermalLimit" "83"
        }
        $content = $content.Replace($block, $newBlock)
    } else {
        Write-Host "$(TS) WARNING: [$pName] section not found — skipping"
    }

    Write-Host "$(TS) $pName -> Core: $([int]$startupCore/1000) MHz | Mem: +$($memKhz/1000) MHz | Fan: ${fanPct}% | VF: Startup curve"
}

Write-Host ""

if ($DryRun) {
    Write-Host "$(TS) DRY RUN complete. No files modified."
    exit 0
}

# Write to temp location — ASCII encoding, no BOM
$tempPath = Join-Path $env:TEMP "AB_profile_modified.cfg"
[System.IO.File]::WriteAllText($tempPath, $content, [System.Text.Encoding]::ASCII)

# Verify no BOM
$firstBytes = [System.IO.File]::ReadAllBytes($tempPath)[0..2]
if ($firstBytes[0] -eq 239 -and $firstBytes[1] -eq 187 -and $firstBytes[2] -eq 191) {
    Write-Host "$(TS) WARNING: BOM detected, stripping..."
    $allBytes = [System.IO.File]::ReadAllBytes($tempPath)
    [System.IO.File]::WriteAllBytes($tempPath, [byte[]]$allBytes[3..($allBytes.Length-1)])
}

$fileSize = (Get-Item $tempPath).Length
Write-Host "$(TS) Written to: $tempPath ($fileSize bytes, ASCII, no BOM)"

# Backup original
$backupSuffix = Get-Date -Format "yyyyMMdd_HHmmss"
$backupPath = Join-Path $env:TEMP "AB_profile_backup_$backupSuffix.cfg"
Copy-Item $cfgPath $backupPath -Force
Write-Host "$(TS) Backup saved: $backupPath"

# Copy to Afterburner dir (needs admin)
Write-Host "$(TS) Copying to Afterburner profile dir (admin required)..."
try {
    Copy-Item $tempPath $cfgPath -Force -ErrorAction Stop
    Write-Host "$(TS) SUCCESS: Config updated directly."
} catch {
    # Elevate
    $psCmd = "Copy-Item '$tempPath' '$cfgPath' -Force; Start-Sleep 1"
    Start-Process powershell -Verb RunAs -ArgumentList "-Command",$psCmd -Wait

    $liveBytes = [System.IO.File]::ReadAllBytes($cfgPath)[0..2]
    if ($liveBytes[0] -eq 91) {
        Write-Host "$(TS) SUCCESS: Config updated via elevation (clean encoding)."
    } else {
        Write-Host "$(TS) WARNING: Copy may have failed. Manual copy:"
        Write-Host "$(TS)   Copy-Item '$tempPath' '$cfgPath' -Force"
    }
}

Write-Host ""
Write-Host "$(TS) Profile layout:"
foreach ($prof in $PROFILES) {
    Write-Host ("$(TS)   {0}: Core {1} (VF curve) | Mem +{2} MHz | Fan {3}%" -f $prof.Name, ([int]$startupCore/1000), ($prof.Mem/1000), $prof.Fan)
}
Write-Host ""
Write-Host "$(TS) Restart MSI Afterburner to apply changes."
