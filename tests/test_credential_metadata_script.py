"""Run the PowerShell resolver against a synthetic op function, never a vault."""

import copy
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
pytestmark = pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell fixture integration")


def fixture():
    return {
        "accounts": [{"account_uuid": "wrong-account"}, {"account_uuid": "mcg-account"}],
        "account_details": {"wrong-account": {"name": "Other"}, "mcg-account": {"name": "MCG LLC"}},
        "vaults": [{"id": "employee-vault", "name": "Employee"}],
        "items": [{"id": "target-item", "title": "signalforce"}, {"id": "wrong-item", "title": "SIGNALFORGE"}],
        "item": {"id": "target-item", "title": "signalforce", "vault": {"id": "employee-vault"}, "fields": [
            {"id": "id-field", "label": "SIGNALFORGE_ACCESS_CLIENT_ID", "type": "STRING", "value": "SYNTHETIC_SECRET_NEVER_EMIT"},
            {"id": "secret-field", "label": "SIGNALFORGE_ACCESS_CLIENT_SECRET", "type": "CONCEALED", "value": "SYNTHETIC_SECRET_NEVER_EMIT"},
            {"id": "ingest-field", "label": "SIGNALFORGE_INGEST_SECRET", "type": "CONCEALED", "value": "SYNTHETIC_SECRET_NEVER_EMIT"},
        ]},
    }


HARNESS = r"""
$ErrorActionPreference = 'Stop'
$global:signalforgeMockFixture = Get-Content -LiteralPath $env:SIGNALFORGE_TEST_FIXTURE -Raw | ConvertFrom-Json
function global:op {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$OpArgs)
    $global:LASTEXITCODE = 0
    $operation = $OpArgs[0] + ' ' + $OpArgs[1]
    $accountIndex = [Array]::IndexOf($OpArgs, '--account')
    $vaultIndex = [Array]::IndexOf($OpArgs, '--vault')
    switch ($operation) {
        'account list' { return ($global:signalforgeMockFixture.accounts | ConvertTo-Json -Depth 10 -Compress) }
        'account get' { return ($global:signalforgeMockFixture.account_details.PSObject.Properties[$OpArgs[$accountIndex+1]].Value | ConvertTo-Json -Depth 10 -Compress) }
        'vault list' {
            if ($OpArgs[$accountIndex+1] -ne 'mcg-account') { throw 'Wrong account' }
            return ($global:signalforgeMockFixture.vaults | ConvertTo-Json -Depth 10 -Compress)
        }
        'item list' {
            if ($OpArgs[$accountIndex+1] -ne 'mcg-account' -or $OpArgs[$vaultIndex+1] -ne 'employee-vault') { throw 'Wrong vault scope' }
            return ($global:signalforgeMockFixture.items | ConvertTo-Json -Depth 10 -Compress)
        }
        'item get' {
            if ($OpArgs[2] -ne 'target-item' -or $OpArgs[$accountIndex+1] -ne 'mcg-account' -or $OpArgs[$vaultIndex+1] -ne 'employee-vault') { throw 'Wrong item scope' }
            return ($global:signalforgeMockFixture.item | ConvertTo-Json -Depth 10 -Compress)
        }
        default { throw 'Unexpected command; this fixture permits reads only' }
    }
}
& $env:SIGNALFORGE_TEST_SCRIPT -OutputPath $env:SIGNALFORGE_TEST_OUTPUT
"""


@pytest.fixture(scope="module")
def script_execution_allowed():
    policy = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
                            capture_output=True, text=True, timeout=15)
    if policy.returncode != 0 or policy.stdout.strip() in {"Restricted", "AllSigned"}:
        pytest.skip("Child PowerShell policy disallows this unsigned script; policy is not changed")


def resolve(tmp_path, data):
    fixture_path = tmp_path / "synthetic-fixture.json"
    output = tmp_path / "metadata.json"
    fixture_path.write_text(json.dumps(data), encoding="utf-8")
    env = {**os.environ,
           "SIGNALFORGE_TEST_FIXTURE": str(fixture_path),
           "SIGNALFORGE_TEST_SCRIPT": str(ROOT / "scripts/Get-SignalforgeCredentialMetadata.ps1"),
           "SIGNALFORGE_TEST_OUTPUT": str(output)}
    result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", HARNESS],
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert output.exists(), (result.stdout, result.stderr)
    assert "SYNTHETIC_SECRET_NEVER_EMIT" not in result.stdout + result.stderr + output.read_text(encoding="utf-8-sig")
    return json.loads(output.read_text(encoding="utf-8-sig"))


def test_resolves_exact_account_vault_and_signalforce_item_without_values(tmp_path, script_execution_allowed):
    result = resolve(tmp_path, fixture())
    assert result["status"] == "resolved_metadata_only"
    assert result["account"]["name"] == "MCG LLC"
    assert result["vault"]["name"] == "Employee"
    assert result["item"]["title"] == "signalforce"
    assert result["auth_fields"]["access_client_id"]["field_id"] == "id-field"
    assert result["auth_fields"]["access_client_secret"]["field_id"] == "secret-field"
    assert all("value" not in field for field in result["fields"])
    assert result["heartbeat_sent"] is False


@pytest.mark.parametrize("role", ["account", "vault", "item"])
def test_ambiguous_identities_are_not_selected(tmp_path, role, script_execution_allowed):
    data = fixture()
    if role == "account":
        data["account_details"]["wrong-account"]["name"] = "MCG LLC"
    elif role == "vault":
        data["vaults"].append(copy.deepcopy(data["vaults"][0]))
    else:
        data["items"].append(copy.deepcopy(data["items"][0]))
    result = resolve(tmp_path, data)
    assert result["status"] == role + "_not_unique_or_unavailable"


def test_generic_password_field_does_not_imply_cloudflare_client_secret(tmp_path, script_execution_allowed):
    data = fixture()
    data["item"]["fields"][1]["label"] = "password"
    result = resolve(tmp_path, data)
    assert result["auth_fields"]["access_client_secret"]["status"] == "unconfirmed"


def test_duplicate_client_id_labels_are_not_confirmed(tmp_path, script_execution_allowed):
    data = fixture()
    data["item"]["fields"].append({"id": "other-id", "label": "Access Client ID", "type": "STRING", "value": "SYNTHETIC_SECRET_NEVER_EMIT"})
    result = resolve(tmp_path, data)
    assert result["auth_fields"]["access_client_id"]["status"] == "unconfirmed"


def test_metadata_scripts_parse_without_executing_them():
    source = """
$errors=$null; $tokens=$null
foreach ($name in @('Get-SignalforgeCredentialMetadata.ps1','Test-SignalforgeAccess.ps1')) {
    [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:SIGNALFORGE_TEST_SCRIPT_DIR $name),[ref]$tokens,[ref]$errors) | Out-Null
    if ($errors.Count -gt 0) { exit 1 }
}
"""
    result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", source],
                            capture_output=True, text=True, timeout=15,
                            env={**os.environ, "SIGNALFORGE_TEST_SCRIPT_DIR": str(ROOT / "scripts")})
    assert result.returncode == 0, result.stderr
