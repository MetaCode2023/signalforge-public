# SIGNALFORGE reporting readiness and Windows networking diagnosis

This is a new engineering status record, **not** the missing deployment
`HANDOFF.md`. Existing data and the untracked `REPORTING_SETUP.md` were retained.
No Cloudflare resources, Access policies, Windows firewall policies, Defender
settings, or live execution were changed. Free market-data sources only.

## Reporting outcome

**No authenticated heartbeat was sent or accepted. No event ID was verified
in Cloudflare/D1 or the private dashboard. Scheduled remote reporting is not
enabled.** Network, secure-item access, and deployed-schema requirements remain.

The existing Site was located through the authorized Sites connector:

- URL: https://signalforge-dashboard.example.com/#market
- Site: `appgprj_6adff6c4a7dc8191b4dfdfe09972f859`, active, version 4.
- Existing custom/private access was retained. No site version was deployed.
- The Site connector exposes no D1 binding for this Site. This does not imply
  the separately deployed Cloudflare `signalforge-research` database is absent.
- Site source Git access was provisioned in session memory, without emitting
  its credential. No credential was written to a file or Git configuration.
  Source inspection still requires an authorized network path to that repository.

## Windows error 10013: observed evidence and limits

Microsoft defines 10013 as WSAEACCES, a socket operation denied by access
permissions: https://learn.microsoft.com/en-us/windows/win32/winsock/windows-sockets-error-codes-2

The failure occurs in the managed Codex terminal process tree. One captured
read-only process snapshot showed:

```text
python.exe PID 16220 (uv Python 3.13.15)
  <- .venv\Scripts\python.exe PID 26280
  <- powershell.exe PID 2980
  <- codex-command-runner-0.165.0.exe PID 9684
     C:\Users\you\.codex\.sandbox-bin\codex-command-runner-0.165.0.exe
  <- codex.exe PID 3148
  <- codex.exe PID 3556
  <- powershell.exe PID 9756
  <- WindowsTerminal.exe PID 7252
```

PIDs are historical observations, not identifiers to terminate. A token query
on a Python child showed `has_restrictions=true`, `is_app_container=false`,
and `is_elevated=false`. The session's configured network access is restricted.

| Diagnostic | Observed |
|---|---|
| DNS for API, dashboard, FRED | Succeeded for all three |
| WinHTTP proxy | Direct access, no proxy server |
| HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY | Absent from the Python process |
| TCP 443 to API/dashboard | Denied with Windows 10013 before TLS or HTTP |
| Sites connector | Successfully read the existing private Site metadata |
| 1Password connector | Authenticated successfully |
| Browser automation | Browser access host unavailable |
| Firewall-profile inspection | Denied by the restricted environment |
| 1Password CLI `op.exe` execution | Access denied before credential lookup |

The evidence locates the restriction at the managed terminal/sandbox boundary,
not DNS, an application proxy, or a Cloudflare HTTP authentication rejection.
Austin's successful package installation from a normal terminal is independent
evidence that the host has an authorized external network path. The exact
Windows Filtering Platform rule/filter ID cannot be determined with the denied
firewall inspection privileges; no specific Defender rule is asserted.

No denied connection was retried after diagnosis. No endpoint restrictions
were bypassed. No claim is made that local code changes resolved this policy.

### Exact access needed

Use a legitimately network-enabled execution profile or an ordinary authorized
Windows terminal. The reporting executor needs outbound TCP/TLS 443 to:

- `signalforge-api.example.com` for documented authenticated ingest.
- `signalforge-dashboard.example.com` for authorized dashboard verification.
- The existing Site's source Git host, `git.chatgpt-team.site`, if source must be
  retrieved to locate its handoff and API integration contract.

Secure lookup also needs permission to execute the installed 1Password CLI and
its supported desktop integration, or a supported connector that can access the
existing vault item. This is **not** a request to disable or widen host firewall,
endpoint protection, or Cloudflare Access policies.

## Secure credential lookup

The 1Password MCP connection authenticated, but its supported interface is
Developer Environments only; `list_environments` returned an empty list. It
does not expose arbitrary vault-item reads, so the existing SIGNALFORGE item
has **not** been resolved. CLI execution was denied. Local `.env` contents and
credential values were not read, printed, logged, committed, or copied.

`scripts/Test-SignalforgeAccess.ps1` is a concrete normal-terminal preflight.
It never sends a heartbeat, changes a security setting, or writes secret values.
With `-InspectVault`, it uses the supported 1Password CLI and emits only the
exact matching item's ID and field identifiers/labels/types. Multiple or zero
exact `SIGNALFORGE` item matches fail safely. Raw CLI JSON stays in process
memory. The script was syntax-checked, not executed here through denied access.

Run it from a normal authorized PowerShell terminal:

```powershell
Set-Location 'C:\Users\you\repos\signalforge'
& '.\scripts\Test-SignalforgeAccess.ps1' -InspectVault
```

It saves metadata only to `data/verification/normal-terminal-access.json`.
If 1Password requests desktop approval, the user completes that normal approval.
Do not paste a secret, disable execution policies, or accept an unplanned upgrade.

Supported CLI documentation:
https://developer.1password.com/docs/cli/secrets-scripts
https://www.1password.dev/cli/reference/management-commands/item

## Handoff and exact API contract

No SIGNALFORGE `HANDOFF.md` was found in the repository, nearby repositories,
Documents/Codex workspaces, Downloads, Desktop, OneDrive, or accessible Codex
worktree/scratch folders. Unrelated projects' handoff files were not substituted.

Local `docs/REPORTING_SETUP.md` documents POST to the existing
`https://signalforge-api.example.com/v1/ingest` using:

- `CF-Access-Client-Id`, `CF-Access-Client-Secret`.
- `x-signalforge-timestamp`, `x-signalforge-nonce`.
- `x-signalforge-signature: v1=<hex HMAC-SHA256>` over the exact UTF-8 bytes
  `v1:<timestamp>:<nonce>:<raw body>`.
- Fresh timestamp/nonce per transmission and no credential-carrying redirects.

`signalforge/reports/auth.py` implements and tests that locally documented
signature/header calculation, with secret-free credential representations.
Synthetic test bodies are explicitly **not** a deployed heartbeat schema.

Still required from the deployed Worker/source/handoff:

1. Allowed heartbeat/report type, envelope and schema version.
2. Required/optional field names, value types/enums, paper/test isolation rules.
3. Event-ID field, stable idempotency identity, duplicate/replay semantics.
4. Successful acceptance response schema and event lookup/verification path.
5. Credential field/reference mapping in the existing 1Password item.

No payload, acceptance response, new endpoint, or duplicate-handling guarantee
was invented. Authentication details above are documented locally; deployed
Worker acceptance is not yet confirmed. Durable scheduled sending/retries and
failure alerts must remain off until this contract is verified, especially
because retrying an ambiguous request can duplicate an event without known
idempotency semantics.

## Ten verified local reliability/safety issues addressed

| Priority | Defect and resulting behavior | Regression coverage |
|---|---|---|
| 1 | A newly created runner could skip persisted sessions yet reset the cash/book. It now refuses new sessions without book restoration; same-runner continuation and completed-range no-ops still work. | `test_restarting_with_a_fresh_cash_book_cannot_advance_sessions`, updated runner tests |
| 2 | Backtest execution overrides used unchecked `model_copy`, bypassing delay/cost/participation validation. Overrides are now validated through `ExecutionConfig`. | `test_execution_overrides_are_revalidated` |
| 3 | Internally consistent margin/leverage flags could reach simulation execution. Both execution entry points now enforce the cash-only mandate and revalidate copied models. | `test_execution_refuses_margin_even_when_config_is_internally_consistent`, paper-mode bypass test |
| 4 | A risk config could enable averaging down on the execution path. Both execution entry points now reject it. | `test_execution_refuses_averaging_down_override` |
| 5 | Provider HTTP requests followed redirects automatically. Redirect following is now disabled, avoiding unintended credential forwarding. | `test_provider_does_not_follow_credential_carrying_redirects` |
| 6 | TLS verification errors inherited `ConnectionError` and were classified as retryable. They now fail permanently without relaxing verification. | `test_tls_errors_are_permanent_and_redacted` |
| 7 | Vendor notices could echo credential-bearing text into exceptions. Notices are now reduced to safe categorical errors. | `test_provider_notices_cannot_echo_credentials` |
| 8 | Corrupt persisted request-budget state could leave a previous healthy status visible. A failed status is now persisted before stopping. | `test_corrupt_request_budget_replaces_stale_healthy_status` |
| 9 | A failed cycle returned only `status`, losing its run identity/timestamp. Callers now receive the same full safe event as the status log. | `test_failed_cycle_returns_the_same_complete_event_as_the_log` |
| 10 | Exceptions during experiments produced no durable failed-attempt record. Failures now retain distinct attempt IDs, spec/version/window/data fingerprint/seed/error class, including repeated failures, without arbitrary exception text. | `test_every_failed_experiment_is_persisted_without_exception_text` |

Failed attempts are stored in the additive `experiment_failures` table. Existing
data is not removed, rewritten, or promoted. This is a fail-closed restart guard,
not a claim that complete book/ledger restoration has been implemented.

## Actual research/data evidence and remaining limits

The supported Python 3.13.15 replay of 506 AAPL bars from 2015-02-17 through
2017-02-16 produced simulated equity 100,000 -> 100,091.171524, one closed trade,
three fills, commissions 3.00, slippage 7.288400, and `INSUFFICIENT_EVIDENCE`.
It is an old one-stock engineering replay, not independent validation or proof
of a tradable edge. Cash, cost assumptions and risk limits were not loosened.

Current persisted main-warehouse coverage (read-only inspection):

- 26,524 bars, 58,969 index/macro observations, 85,494 immutable raw records.
- AAPL sample: 506 bars, 2015-02-17 through 2017-02-16.
- MarketParquet: 6,799 symbols, 26,018 accepted bars, only four sessions
  (2026-10-20 through 2026-10-23).
- 1 quarantined record and 58 quality flags.
- Fresh OHLCV, delisted-history coverage, and survivorship-bias correction remain
  unavailable. Those are data limitations, not issues fixed by risk-code edits.

Focused verification before the full suite: 224 passed, 1 network test
deselected, 21 subtests passed. Signing/safety checks: 27 passed. Final full
suite on Python 3.13.15: **1,228 passed, 1 network test deselected, 31 subtests
passed in 259.57 seconds**. There were no failing tests. The report is
`data/verification/reporting-full-suite.xml`. Syntax compilation and Git
whitespace checks also passed. The PowerShell preflight passed parser
validation only; its normal-terminal connectivity/item lookup is not claimed
to have been executed.

## Next verified operational step

Run the metadata-only normal-terminal preflight above and obtain the actual
deployment `HANDOFF.md`/schema through an authorized source path. Once both
are available, implement the contract-specific durable sender and schedule,
send exactly one authenticated paper-only heartbeat, and verify the same event
ID in acceptance/storage and the private dashboard before enabling repetition.
Never label this reporting pipeline complete based on local signing tests.
