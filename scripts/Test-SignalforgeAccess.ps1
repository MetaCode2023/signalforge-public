param(
    [switch]$InspectVault,
    [string]$AccountName = 'MCG LLC',
    [string]$VaultName = 'Employee',
    [string]$ItemTitle = 'signalforce'
)

# Run in a normal authorized Windows terminal. This never sends a report,
# changes network/security settings, or writes secret values to disk/stdout.
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDirectory
$safeReport = [ordered]@{
    checked_at_utc = [DateTime]::UtcNow.ToString('o')
    process_id = $PID
    handoff_at_repository_root = Test-Path -LiteralPath (Join-Path $projectDirectory 'HANDOFF.md')
    network = @()
    vault = @{ status = 'not_requested' }
    heartbeat_sent = $false
}

foreach ($targetHost in @('signalforge-api.example.com', 'signalforge-dashboard.example.com')) {
    $networkResult = [ordered]@{ host = $targetHost; dns = 'unknown'; tcp_443 = 'unknown' }
    try {
        $networkResult.dns = if ([System.Net.Dns]::GetHostAddresses($targetHost).Count -gt 0) { 'ok' } else { 'empty' }
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $connection = $client.ConnectAsync($targetHost, 443)
            if (-not $connection.Wait(5000)) { throw [TimeoutException]::new() }
            $networkResult.tcp_443 = 'ok'
        } finally { $client.Dispose() }
    } catch {
        $networkResult.tcp_443 = 'failed'
        $networkResult.error_type = $_.Exception.GetType().Name
        $baseError = $_.Exception.GetBaseException()
        if ($baseError -is [System.Net.Sockets.SocketException]) {
            $networkResult.socket_error = $baseError.NativeErrorCode
        }
    }
    $safeReport.network += $networkResult
}

if ($InspectVault) {
    try {
        $metadataScript = Join-Path $PSScriptRoot 'Get-SignalforgeCredentialMetadata.ps1'
        $safeMetadata = & $metadataScript -AccountName $AccountName -VaultName $VaultName -ItemTitle $ItemTitle
        $safeReport.vault = ($safeMetadata -join "`n") | ConvertFrom-Json
    } catch {
        $safeReport.vault = @{ status = 'unavailable'; error_type = $_.Exception.GetType().Name }
    }
}

$reportDirectory = Join-Path $projectDirectory 'data\verification'
[System.IO.Directory]::CreateDirectory($reportDirectory) | Out-Null
$safeJson = $safeReport | ConvertTo-Json -Depth 8
$safeJson | Set-Content -LiteralPath (Join-Path $reportDirectory 'normal-terminal-access.json') -Encoding UTF8
Write-Output $safeJson
