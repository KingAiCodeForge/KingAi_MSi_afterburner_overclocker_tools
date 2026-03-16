# Decode MSI Afterburner VF (Voltage-Frequency) curves from .cfg profile files.
#
# Reads the per-GPU config file, decodes the binary hex VF curve data for each
# profile section, and prints a human-readable table of voltage/frequency points.
#
# Compatible with NVIDIA GTX 10-series (Pascal) and newer.
#
# Usage:
#     .\decode_vf_curve.ps1
#     .\decode_vf_curve.ps1 -ConfigPath "path\to\custom.cfg"
#     .\decode_vf_curve.ps1 -ShowAll

param(
    [string]$ConfigPath,
    [switch]$ShowAll
)

# ── Constants ────────────────────────────────────────────────────────────────
$_PFx86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
if (-not $_PFx86) { $_PFx86 = "C:\Program Files (x86)" }
$AB_PROFILES_DIR = if ($env:AB_PROFILES_DIR) { $env:AB_PROFILES_DIR } else { Join-Path $_PFx86 "MSI Afterburner\Profiles" }
$HEX_PER_POINT  = 24       # 12 bytes * 2 hex chars
$HEADER_HEX_LEN = 16       # 8 bytes * 2 hex chars
$SECTIONS = @("Startup","Profile1","Profile2","Profile3","Profile4","Profile5")

function TS { return (Get-Date -Format "[yyyy-MM-dd HH:mm:ss]") }

function Read-Float {
    param([string]$hex, [int]$hexOffset)
    $bytes = @()
    for ($b = 0; $b -lt 4; $b++) {
        $bytes += [Convert]::ToByte($hex.Substring($hexOffset + $b * 2, 2), 16)
    }
    return [BitConverter]::ToSingle([byte[]]$bytes, 0)
}

function Find-Config {
    if ($ConfigPath -and (Test-Path $ConfigPath)) { return $ConfigPath }
    if ($ConfigPath) { Write-Host "$(TS) ERROR: Config not found: $ConfigPath"; exit 1 }

    $matches = Get-ChildItem "$AB_PROFILES_DIR\VEN_10DE*.cfg" -ErrorAction SilentlyContinue
    if (-not $matches) {
        Write-Host "$(TS) ERROR: No NVIDIA config found in $AB_PROFILES_DIR"
        Write-Host "$(TS) Is MSI Afterburner installed? Has it been run at least once?"
        exit 1
    }
    $best = $matches | Sort-Object Length -Descending | Select-Object -First 1
    if ($matches.Count -gt 1) {
        Write-Host "$(TS) Found $($matches.Count) GPU configs, using largest: $($best.Name)"
    }
    return $best.FullName
}

function Decode-VFCurve {
    param([string]$hex)

    if ($hex.Length -lt $HEADER_HEX_LEN) { return @() }

    $countBytes = @()
    for ($j = 0; $j -lt 4; $j++) {
        $countBytes += [Convert]::ToByte($hex.Substring(8 + $j * 2, 2), 16)
    }
    $pointCount = [BitConverter]::ToInt32($countBytes, 0)
    $data = $hex.Substring($HEADER_HEX_LEN)
    $maxPoints = [math]::Min($pointCount, [math]::Floor($data.Length / $HEX_PER_POINT))

    $results = @()
    for ($i = 0; $i -lt $maxPoints; $i++) {
        $offset = $i * $HEX_PER_POINT
        $adj  = Read-Float $data $offset
        $volt = Read-Float $data ($offset + 8)
        $freq = Read-Float $data ($offset + 16)
        $results += [PSCustomObject]@{
            Index      = $i
            Voltage    = [math]::Round($volt, 2)
            Frequency  = [math]::Round($freq, 1)
            Adjustment = [math]::Round($adj, 1)
        }
    }
    return $results
}

# ── Main ─────────────────────────────────────────────────────────────────────
$cfgPath = Find-Config
Write-Host "$(TS) Reading config: $(Split-Path $cfgPath -Leaf)"
Write-Host "$(TS) Full path: $cfgPath"
Write-Host ""

$cfg = Get-Content $cfgPath -Raw

foreach ($sec in $SECTIONS) {
    $pattern = [regex]::Escape("[$sec]")
    $secMatch = [regex]::Match($cfg, "$pattern([\s\S]*?)(?=\[(?:Startup|Profile\d|PreSuspendedMode|Defaults|Settings)\]|\z)")
    if (-not $secMatch.Success) {
        Write-Host "$(TS) === $sec === (not found in config)"
        Write-Host ""
        continue
    }

    $block = $secMatch.Groups[1].Value

    $core = if ($block -match "CoreClkBoost=(-?\d+)") { [int]$Matches[1] / 1000 } else { "N/A" }
    $mem  = if ($block -match "MemClkBoost=(-?\d+)")  { "+$([int]$Matches[1] / 1000)" } else { "N/A" }
    $pwr  = if ($block -match "PowerLimit=(\d+)")     { $Matches[1] } else { "N/A" }
    $therm = if ($block -match "ThermalLimit=(\d+)")   { $Matches[1] } else { "N/A" }
    $fanMode = if ($block -match "FanMode=(\d+)")      { $Matches[1] } else { "N/A" }
    $fanSpd  = if ($block -match "FanSpeed=(\d+)")     { $Matches[1] } else { "N/A" }
    $fanLabel = switch ($fanMode) { "0" { "auto" } "1" { "manual" } default { $fanMode } }

    Write-Host "$(TS) $('=' * 50)"
    Write-Host "$(TS) === $sec ==="
    Write-Host "$(TS)   Core: $core MHz | Mem: $mem MHz | Power: ${pwr}% | Thermal: ${therm}C"
    Write-Host "$(TS)   Fan: ${fanSpd}% ($fanLabel)"
    Write-Host ""

    $vfMatch = [regex]::Match($block, "VFCurve=([0-9A-Fa-f]+)")
    if (-not $vfMatch.Success) {
        Write-Host "$(TS)   No VF curve data"
        Write-Host ""
        continue
    }

    $vfHex = $vfMatch.Groups[1].Value.Trim()
    $points = Decode-VFCurve $vfHex

    if ($points.Count -eq 0) {
        Write-Host "$(TS)   VF curve present but could not decode ($($vfHex.Length) hex chars)"
        Write-Host ""
        continue
    }

    Write-Host "$(TS)   VF Curve ($($points.Count) points, 12 bytes/point, $($vfHex.Length) hex chars):"
    Write-Host ("$(TS)   {0,5}  {1,10}  {2,10}  {3,10}" -f "Idx", "Voltage", "Frequency", "Adjustment")
    Write-Host ("$(TS)   {0,5}  {1,10}  {2,10}  {3,10}" -f "-----", "-------", "---------", "----------")

    $prevFreq = -1.0
    foreach ($pt in $points) {
        $isInflection = $pt.Frequency -ne $prevFreq
        $isEdge = ($pt.Index -eq 0) -or ($pt.Index -ge ($points.Count - 2))

        if ($ShowAll -or $isInflection -or $isEdge) {
            $marker = ""
            if ($prevFreq -gt 0 -and ($pt.Frequency - $prevFreq) -lt -50) {
                $marker = "  <-- undervolt clamp"
            }
            Write-Host ("$(TS)   [{0,3}]  {1,8:F1} mV  {2,8:F1} MHz  (adj: {3,6:F1}){4}" -f $pt.Index, $pt.Voltage, $pt.Frequency, $pt.Adjustment, $marker)
        }
        $prevFreq = $pt.Frequency
    }

    $voltages = $points | ForEach-Object { $_.Voltage }
    $freqs = $points | ForEach-Object { $_.Frequency }
    $minV = ($voltages | Measure-Object -Minimum).Minimum
    $maxV = ($voltages | Measure-Object -Maximum).Maximum
    $maxF = ($freqs | Measure-Object -Maximum).Maximum
    Write-Host ""
    Write-Host "$(TS)   Range: $minV–$maxV mV | Peak: $maxF MHz"
    Write-Host ""
}
