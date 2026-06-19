# Authentik Disaster Plan (48h Exit)

If Authentik has an unpatched critical vulnerability or becomes commercially unusable, we switch IdP within 48 hours while preserving tenant/app behavior.

## Triggers
- Unpatched critical Authentik CVE with public exploit.
- Licensing or service change that blocks production use.
- Repeated production incidents with no near-term remediation path.

## Immediate Actions (Hour 0-2)
1. Freeze Authentik config changes.
2. Snapshot Authentik DB and archive current compose/nginx state.
3. Notify ops channel and set customer-facing status message if login impact is expected.
4. Choose replacement IdP: Auth0, Keycloak, or Zitadel.

## Cutover Plan (Hour 2-48)
1. Stand up replacement IdP in parallel.
2. Recreate OAuth/OIDC apps with equivalent redirect URIs and scopes.
3. Update platform env vars and issuer/JWKS configuration to new IdP.
4. Force password reset for migrated users (no hash portability assumption).
5. Run smoke tests:
   - interactive login
   - token mint
   - JWKS validation
   - tenant claim propagation
6. Shift traffic by updating nginx and app auth settings, then monitor errors/latency.

## Data and Rollback
- Keep Authentik DB backups for at least 90 days after cutover.
- If cutover fails, restore previous Authentik containers from backup and revert env/nginx.
- Do not delete Authentik volumes until new IdP is stable for 7 days.

## Minimum Evidence of Success
- Admin and user login succeed with the new IdP.
- Access tokens validate in API and tenant-aware routes.
- No auth-related SEV incidents for 24 hours after cutover.
