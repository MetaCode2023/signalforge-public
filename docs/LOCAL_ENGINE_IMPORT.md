# SIGNALFORGE local engine import checklist

This repository currently contains a paper-only Cloudflare reporting API **reference implementation** in `integrations/cloudflare-research-api/`. It is **not evidence** of the code currently deployed in Cloudflare. A separate Windows workstation contains the broader Python engine at `C:\\Users\\you\\repos\\signalforge`.

Before importing that Windows worktree:

1. Inspect `git remote -v`, `git branch -avv`, `git status --short`, and existing commit history. Preserve history, branches, and all uncommitted user work.
2. Verify the repository name and confirm this private GitHub target is intended. Never force-push or overwrite main.
3. Audit staged files for API keys, passwords, tokens, private certificates, account identifiers, logs, local caches, and licensed market data. Do not commit any secrets or raw restricted datasets.
4. Add targeted ignore patterns for environment files, cache directories, local databases, output artifacts, and downloaded datasets, without excluding source or test fixtures unnecessarily.
5. Bring the actual Python trading engine into a separate feature branch and open a PR. Keep existing module paths where practical.
6. Run the full Python test suite and confirm paper-only, no broker order submission, martingale controls, reproducible backtesting, and restart persistence.
7. Obtain the verified *deployed* API contract from Cloudflare and reconcile it with the reference code and terminal client before enabling scheduled reporting.
8. Require PR review and passing tests. Do not deploy, merge, alter risk limits, or enable live trading without explicit authorization.

The initial GitHub import should be treated as a source-control migration, **not** a production deployment.
