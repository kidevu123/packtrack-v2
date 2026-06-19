# Pack Track follow-ups (post v2.2.0)

Captured during the Pack Track receive integration closeout (June 2026).
Each file is a self-contained spec ready to become a GitHub issue or
sprint card — the team can lift the text verbatim.

| | Title | Severity | Spans |
|---|---|---|---|
| [A](A-zoho-direct-write-migration.md) | Move `push_po` and `adjust_stock` behind zoho-integration-service | medium | packtrack-v2, zoho-integration-service |
| [B](B-deploy-migration-erasure-guard.md) | Stop `deploy/deploy.sh --delete` from erasing LXC-only files | high | packtrack-v2 |
| [C](C-boomin-brand-support.md) | Multi-brand support: keep Pack Track `ZOHO_INTEGRATION_BRAND`-driven, add Boomin | low | packtrack-v2, zoho-integration-service |
| [D](D-live-write-runbook.md) | Live-write window runbook (allowlist + flag flip, with auto-close safeguard) | high | zoho-integration-service, ops |

The numbering matches the closeout brief. Convert to issues with
`gh issue create --body-file docs/followups/X-…md` once the team uses
GitHub Issues for this repo.
