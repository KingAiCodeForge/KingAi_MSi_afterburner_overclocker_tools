#requires -Version 5.1
<#
.SYNOPSIS
Plans a guarded MSI Afterburner profile-file transaction.

.DESCRIPTION
This PowerShell entry point delegates validation and file transactions to the
stdlib-only Python implementation. It never scans for or guesses a GPU config.
Without -Execute it prints a plan and does not modify profile files.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [Parameter(Mandatory = $true)]
    [string]$GpuId,

    [ValidateSet("Startup", "Profile1", "Profile2", "Profile3", "Profile4", "Profile5")]
    [string]$SourceProfile = "Startup",

    [ValidateSet("Profile1", "Profile2", "Profile3", "Profile4", "Profile5")]
    [string[]]$Profile = @("Profile1", "Profile2", "Profile3", "Profile4", "Profile5"),

    [double]$Core,

    [ValidateCount(5, 5)]
    [int[]]$Memory,

    [ValidateCount(5, 5)]
    [int[]]$Fan,

    [int]$Power,
    [int]$Thermal,
    [string]$BackupDir,
    [switch]$AcknowledgeSourceProfile,
    [switch]$AcknowledgeAfterburnerClosed,
    [switch]$Execute,
    [switch]$Json,
    [string]$PythonCommand = "python"
)

$scriptPath = Join-Path $PSScriptRoot "apply_profiles.py"
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    Write-Error "Python implementation not found: $scriptPath"
    exit 2
}

$arguments = New-Object "System.Collections.Generic.List[string]"
$arguments.Add($scriptPath)
$arguments.Add("--config")
$arguments.Add($ConfigPath)
$arguments.Add("--gpu-id")
$arguments.Add($GpuId)
$arguments.Add("--source-profile")
$arguments.Add($SourceProfile)

foreach ($name in $Profile) {
    $arguments.Add("--profile")
    $arguments.Add($name)
}

if ($PSBoundParameters.ContainsKey("Core")) {
    $arguments.Add("--core")
    $arguments.Add($Core.ToString([Globalization.CultureInfo]::InvariantCulture))
}

if ($PSBoundParameters.ContainsKey("Memory")) {
    $arguments.Add("--memory")
    foreach ($value in $Memory) {
        $arguments.Add($value.ToString([Globalization.CultureInfo]::InvariantCulture))
    }
}

if ($PSBoundParameters.ContainsKey("Fan")) {
    $arguments.Add("--fan")
    foreach ($value in $Fan) {
        $arguments.Add($value.ToString([Globalization.CultureInfo]::InvariantCulture))
    }
}

if ($PSBoundParameters.ContainsKey("Power")) {
    $arguments.Add("--power")
    $arguments.Add($Power.ToString([Globalization.CultureInfo]::InvariantCulture))
}
if ($PSBoundParameters.ContainsKey("Thermal")) {
    $arguments.Add("--thermal")
    $arguments.Add($Thermal.ToString([Globalization.CultureInfo]::InvariantCulture))
}
if ($AcknowledgeSourceProfile) {
    $arguments.Add("--acknowledge-source-profile")
}
if ($AcknowledgeAfterburnerClosed) {
    $arguments.Add("--acknowledge-afterburner-closed")
}

if ($BackupDir) {
    $arguments.Add("--backup-dir")
    $arguments.Add($BackupDir)
}
if ($Execute) {
    $arguments.Add("--execute")
}
if ($Json) {
    $arguments.Add("--json")
}

& $PythonCommand @arguments
exit $LASTEXITCODE
