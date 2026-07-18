param(
    [string]$Config = (Join-Path $PSScriptRoot "config.json"),
    [string]$HostOverride = "",
    [int]$PortOverride = 0
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$arguments = @("-m", "gdl_backend", "--config", $Config)
if ($HostOverride) {
    $arguments += @("--host", $HostOverride)
}
if ($PortOverride -gt 0) {
    $arguments += @("--port", [string]$PortOverride)
}

& python @arguments
exit $LASTEXITCODE
