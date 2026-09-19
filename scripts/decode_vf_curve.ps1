#requires -Version 5.1
<#
.SYNOPSIS
Decodes an explicitly selected MSI Afterburner VF curve.

.DESCRIPTION
The selected config filename must exactly match -GpuId. This read-only wrapper
delegates binary decoding to the stdlib-only Python implementation.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [Parameter(Mandatory = $true)]
    [string]$GpuId,

    [switch]$ShowAll,
    [string]$PythonCommand = "python"
)

$scriptPath = Join-Path $PSScriptRoot "decode_vf_curve.py"
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
if ($ShowAll) {
    $arguments.Add("--all")
}

& $PythonCommand @arguments
exit $LASTEXITCODE
