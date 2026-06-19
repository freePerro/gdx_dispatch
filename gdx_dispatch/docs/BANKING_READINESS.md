# Banking Readiness Checklist

> **STATUS: INVALIDATED 2026-05-05 — needs full re-do against current system.**
>
> The previous checklist (12/12 ✅, dated 2026-03-31 / 2026-04-01) was found to be invalid on 2026-05-05:
>
> - All 3 evidence files referenced (`automated_security_20260331.txt`, `qwen_security_chain_20260331.txt`, `resilience_chain_20260331.txt`) no longer exist in the repo.
> - 3 of 5 referenced test files were renumbered to unrelated tests — the `test_11_*`, `test_12_*`, `test_26_*` slots now hold infrastructure / secrets+HA / contractors+whitelabel tests, not the api-contracts / accessibility / dependency-startup tests the checklist claimed passed.
> - 2,157 commits landed between 2026-04-01 and 2026-05-05 with no checklist update.
> - At least two ticks were retroactively falsified by post-snapshot findings: the audit-chain (item #4) was broken by the S100 `cc_staff_audit_log.row_hash` shape mismatch (fixed 2026-05-04); IAM (item #1) has three live structural gaps named in `ai-queue/plans/sprint_auth_identity_hardening.md`.
>
> **Do not cite this file as evidence of banking readiness.** A fresh chain — re-run against the current codebase, with evidence files committed alongside the checklist — is required before any banking partner conversation.
>
> When rebuilding, consider wiring each item to a re-runnable script under `gdx_dispatch/tools/banking_readiness/` so the checklist can never silently rot again (each ✅ becomes "last passed: <date>" with a stale-after-30-days gate).

---

## Checklist (TO BE REBUILT)

The 12 domains below are kept as a starting structure for the re-do. **All checkboxes intentionally unchecked** — each requires fresh evidence against the current codebase before being marked complete.

1. [ ] IAM hardening (MFA, session timeout, privileged re-auth, refresh-token DB-verify, role from DB not JWT). — *evidence TBD*
2. [ ] Authorization and tenant isolation verified for all protected routes (three-plane model: tenant DB-per-tenant, control RLS, commerce dual-party RLS). — *evidence TBD*
3. [ ] Input validation and injection defenses verified across API and forms. — *evidence TBD*
4. [ ] Immutable/tamper-evident audit trail (PG triggers + hash-chain integrity for both `audit_logs` and `cc_staff_audit_log`). — *evidence TBD*
5. [ ] Transaction safety and data integrity. — *evidence TBD*
6. [ ] UI rendering/usability/accessibility baseline. — *evidence TBD*
7. [ ] End-to-end business workflows (lead → customer → job → estimate → invoice → payment → QBO sync). — *evidence TBD*
8. [ ] API contracts and types stable for core endpoints (OpenAPI drift gate green). — *evidence TBD*
9. [ ] Dependency failure and graceful-degradation behavior. — *evidence TBD*
10. [ ] Notification and webhook reliability/failover. — *evidence TBD*
11. [ ] Backup/restore and rollback readiness (re-run restore drill against current schema). — *evidence TBD*
12. [ ] Release gate meta checks and timing budgets. — *evidence TBD*
