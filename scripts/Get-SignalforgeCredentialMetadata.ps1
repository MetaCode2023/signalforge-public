param(
    [string]$AccountName = 'MCG LLC',
    [string]$VaultName = 'Employee',
    [string]$ItemTitle = 'signalforce',
    [string]$OutputPath = ''
)

# Supported, read-only 1Password CLI lookup. Never emit raw item JSON or values.
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) { $OutputPath = Join-Path $projectDirectory 'data\verification\credential-metadata.json' }
$report = [ordered]@{
    checked_at_utc = [DateTime]::UtcNow.ToString('o')
    expected = @{ account = $AccountName; vault = $VaultName; item = $ItemTitle }
    status = 'unavailable'
    secret_values_emitted = $false
    heartbeat_sent = $false
}

function Invoke-OpJson {
    param([string[]]$Arguments)
    $raw = $null
    try {
        $raw = & $script:opCommand @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) { throw [InvalidOperationException]::new('1Password lookup failed') }
        return (($raw -join "`n") | ConvertFrom-Json)
    } finally { $raw = $null }
}

function Get-ExplicitFieldMapping {
    param($Fields, [string[]]$Aliases, [string]$VaultId, [string]$ItemId)
    $matches = @($Fields | Where-Object {
        $label = ([string]$_.label).ToLowerInvariant() -replace '[^a-z0-9]', ''
        $fieldId = ([string]$_.id).ToLowerInvariant() -replace '[^a-z0-9]', ''
        ($Aliases -contains $label) -or ($Aliases -contains $fieldId)
    })
    if ($matches.Count -ne 1) { return @{ status = 'unconfirmed'; matches = $matches.Count } }
    return @{
        status = 'confirmed_by_field_name'
        field_id = $matches[0].id
        label = $matches[0].label
        reference = ('op://{0}/{1}/{2}' -f $VaultId, $ItemId, $matches[0].id)
    }
}

$item = $null
try {
    $script:opCommand = Get-Command op -ErrorAction Stop
    $accounts = @(Invoke-OpJson -Arguments @('account', 'list', '--format=json'))
    $matchingAccounts = @()
    foreach ($account in $accounts) {
        $selector = $account.account_uuid
        if (-not $selector) { $selector = $account.id }
        if (-not $selector) { $selector = $account.url }
        if (-not $selector) { continue }
        try {
            $details = Invoke-OpJson -Arguments @('account', 'get', '--account', $selector, '--format=json')
            $name = $details.name
            if (-not $name) { $name = $details.account_name }
            if (-not $name -and $details.account) { $name = $details.account.name }
            if ($name -ceq $AccountName) { $matchingAccounts += @{ selector = $selector; name = $name } }
        } catch { continue }
    }
    if ($matchingAccounts.Count -ne 1) {
        $report.status = 'account_not_unique_or_unavailable'
        $report.matches = $matchingAccounts.Count
    } else {
        $selectedAccount = $matchingAccounts[0]
        $vaults = @(Invoke-OpJson -Arguments @('vault', 'list', '--account', $selectedAccount.selector, '--format=json'))
        $matchingVaults = @($vaults | Where-Object { $_.name -ceq $VaultName })
        if ($matchingVaults.Count -ne 1) {
            $report.status = 'vault_not_unique_or_unavailable'
            $report.matches = $matchingVaults.Count
        } else {
            $vaultId = $matchingVaults[0].id
            $items = @(Invoke-OpJson -Arguments @('item', 'list', '--account', $selectedAccount.selector, '--vault', $vaultId, '--format=json'))
            $matchingItems = @($items | Where-Object { $_.title -ceq $ItemTitle })
            if ($matchingItems.Count -ne 1) {
                $report.status = 'item_not_unique_or_unavailable'
                $report.matches = $matchingItems.Count
            } else {
                $item = Invoke-OpJson -Arguments @('item', 'get', $matchingItems[0].id, '--account', $selectedAccount.selector, '--vault', $vaultId, '--format=json')
                if ($item.title -cne $ItemTitle -or $item.vault.id -ne $vaultId) {
                    throw [InvalidOperationException]::new('Item identity did not match')
                }
                $report.status = 'resolved_metadata_only'
                $report.account = @{ name = $AccountName; selector = $selectedAccount.selector }
                $report.vault = @{ name = $VaultName; id = $vaultId }
                $report.item = @{ title = $ItemTitle; id = $item.id }
                $report.fields = @($item.fields | ForEach-Object { @{ id = $_.id; label = $_.label; type = $_.type } })
                $idMapping = Get-ExplicitFieldMapping $item.fields @('signalforgeaccessclientid','cloudflareaccessclientid','accessclientid','cfaccessclientid','clientid') $vaultId $item.id
                $secretMapping = Get-ExplicitFieldMapping $item.fields @('signalforgeaccessclientsecret','cloudflareaccessclientsecret','accessclientsecret','cfaccessclientsecret','clientsecret') $vaultId $item.id
                if ($idMapping.field_id -and $idMapping.field_id -eq $secretMapping.field_id) {
                    $idMapping = @{ status = 'unconfirmed'; reason = 'same_field_for_both_roles' }
                    $secretMapping = @{ status = 'unconfirmed'; reason = 'same_field_for_both_roles' }
                }
                $report.auth_fields = @{
                    access_client_id = $idMapping
                    access_client_secret = $secretMapping
                    ingest_signing_secret = Get-ExplicitFieldMapping $item.fields @('signalforgeingestsecret','ingestsecret','hmacingestsecret') $vaultId $item.id
                }
            }
        }
    }
} catch {
    $report.status = 'unavailable'
    $report.error_type = $_.Exception.GetType().Name
} finally { $item = $null }

$outputDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($OutputPath))
[System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
$safeJson = $report | ConvertTo-Json -Depth 10
$safeJson | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Output $safeJson
