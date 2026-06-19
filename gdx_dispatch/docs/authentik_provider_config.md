# Authentik Provider Configuration — GDX

Reference for the GDX-specific Authentik provider set. This document
covers **SS-6 Slice A** (`gdx-spa` provider, landed), the **SS-6 Slice B**
MCP disposition (PAT-only, no Authentik provider), and **SS-6 Slice C**
(`gdx-thirdparty` provider, landed).

> **tl;dr for operators:** `gdx-mcp` is **not** an Authentik OAuth
> provider. `configure_authentik.py` has no `gdx-mcp` choice; do not
> attempt to add one. MCP authentication is PAT bearer — see SS-14 /
> SS-19. Full reasoning in the [MCP Disposition](#mcp-disposition-ss-6-slice-b)
> section below.

## Provider scope

| Provider          | Audience  | Access TTL | Refresh TTL | Flow                   | Used by                        |
|-------------------|-----------|------------|-------------|------------------------|--------------------------------|
| `gdx-spa`         | `gdx-api` | 15 minutes | 30 days     | OAuth 2.1 + S256 PKCE  | Vue SPA login (SS-12)          |
| `gdx-thirdparty`  | `gdx-api` | 1 hour     | 90 days     | OAuth 2.1 + S256 PKCE  | Zapier-class integrations (SS-21) |

Configured artifacts per provider:

- Signing key pair: `{provider}-signing-key` (managed label equals the
  provider name). Separate key per provider so rotation/revocation of
  one does not invalidate tokens minted by the other.
- Property mapping (scope): `gdx_tid` — shared across providers, emits
  the singular active-tenant claim required by the D-5 token shape.
- OAuth2 provider: one per row in the table above, with the exact TTLs
  and audience listed.
- Application: `app-{provider}` (slug `{provider}`) wrapping the
  provider so the OIDC discovery document is published at
  `https://auth.example.com/application/o/{provider}/.well-known/openid-configuration`.

## CLI usage

Prerequisites:

- `AUTHENTIK_BOOTSTRAP_TOKEN` set to an Authentik admin API token.
- `AUTHENTIK_BASE_URL` set (defaults to `https://auth.example.com`).

Configure the `gdx-spa` provider:

```bash
export AUTHENTIK_BOOTSTRAP_TOKEN=<admin-api-token>
AUTHENTIK_BASE_URL=https://auth.example.com \
    python gdx_dispatch/tools/configure_authentik.py --provider gdx-spa
```

Configure the `gdx-thirdparty` provider:

```bash
export AUTHENTIK_BOOTSTRAP_TOKEN=<admin-api-token>
AUTHENTIK_BASE_URL=https://auth.example.com \
    python gdx_dispatch/tools/configure_authentik.py --provider gdx-thirdparty
```

Dry-run (prints the provider payload without touching Authentik):

```bash
python gdx_dispatch/tools/configure_authentik.py --provider gdx-spa --dry-run
python gdx_dispatch/tools/configure_authentik.py --provider gdx-thirdparty --dry-run
```

Add `--verbose` to see each `get_or_create` decision at INFO level.

## Idempotency guarantee

Every resource the script touches is looked up by a deterministic
identifier before being created:

| Resource         | Endpoint                       | Lookup key | `gdx-spa` value          | `gdx-thirdparty` value          |
|------------------|--------------------------------|------------|--------------------------|---------------------------------|
| Signing keypair  | `crypto/certificatekeypairs/`  | `name`     | `gdx-spa-signing-key`    | `gdx-thirdparty-signing-key`    |
| Scope mapping    | `propertymappings/scope/`      | `name`     | `gdx_tid` (shared)       | `gdx_tid` (shared)              |
| OAuth2 provider  | `providers/oauth2/`            | `name`     | `gdx-spa`                | `gdx-thirdparty`                |
| Application      | `core/applications/`           | `slug`     | `gdx-spa`                | `gdx-thirdparty`                |

If a record already exists, the script reuses its primary key. A
`POST` only happens when the lookup returns empty. Running the command
twice on the same Authentik instance therefore produces zero duplicate
artifacts — this is asserted by the `get_or_create` helper (it scans
every result in the `GET` response for an exact `search_field`/`search_value`
match before issuing the `POST`).

## Token shape and the D18 `'human'` assumption

Per D-5, the access token carries a **singular** active-tenant claim
(`gdx_tid`) — never a `tenants[]` array. The property mapping that
emits this claim lives in
`gdx_dispatch/tools/authentik_property_mapping_gdx_tid.py` and is uploaded
verbatim via the `SANDBOX_EXPRESSION` constant.

### D18: Identity.type column is absent today

The platform `identities` table
(`gdx_dispatch/models/platform.py:Identity`) does **not** currently include an
`Identity.type` column. D18 tracks adding that column so human users,
service identities, and agent identities can be distinguished in token
issuance and policy enforcement.

Because D18 has not landed, Slice A makes the following explicit
assumption, captured in code as
`ASSUMED_IDENTITY_TYPE = "human"` in
`gdx_dispatch/tools/authentik_property_mapping_gdx_tid.py`:

> Every Authentik-linked identity is treated as `'human'` for token
> emission purposes until D18 adds `Identity.type`. The mapping does
> not consult `Identity.type` at all today and does **not** emit an
> `identity_type` claim.

When D18 lands, the follow-up work is:

1. Populate `user.attributes['identity_type']` via the GDX sync job
   (currently syncs `memberships` and `active_tenant`).
2. Extend the property mapping to include that value as an
   `identity_type` claim.
3. Remove the `ASSUMED_IDENTITY_TYPE` fallback once the SS-7 validator
   starts enforcing the claim.

These steps will be queued as a bounded slice under the D18 follow-up.

## Verification after running

After running the configure command against a live Authentik instance,
verify **both** providers:

```bash
# 1. Discovery docs — loop over both providers (should return valid OIDC JSON)
for p in gdx-spa gdx-thirdparty; do
  curl -s https://auth.example.com/application/o/$p/.well-known/openid-configuration | jq .
done

# 2. JWKS endpoints — loop over both providers (should return RSA key with alg=RS256)
for p in gdx-spa gdx-thirdparty; do
  curl -s https://auth.example.com/application/o/$p/jwks/ | jq '.keys[0]'
done

# 3. Property mappings (should list gdx_tid with scope_name=gdx_tid — one scope, shared)
# Admin UI → Customization → Property Mappings

# 4. Providers list (should show 2 OAuth2 providers: gdx-spa, gdx-thirdparty; NO gdx-mcp)
# Admin UI → Applications & Sources → Providers
```

Per-provider spot-checks in the Authentik admin UI:

- `gdx-spa`: access 15 min / refresh 30 days / PKCE required / single
  redirect `https://app.example.com/auth/callback`.
- `gdx-thirdparty`: access 1 hour / refresh 90 days / PKCE required /
  single redirect
  `https://integrations.example.com/oauth/callback`.

The token-shape contract (`sub`, `aud=gdx-api`, expiry, `gdx_tid`; no
`tenants[]`; PKCE required; `response_types=["code"]` only) is enforced
deterministically for both providers by
`gdx_dispatch/tests/test_authentik_token_shape.py`.

## MCP Disposition (SS-6 Slice B)

SS-6 Slice B is a **documentation / planning reconciliation slice**. It
records the architectural decision that `gdx-mcp` is **out of scope for
Authentik provider configuration**. No Authentik provider, no signing
key, no Application wrapper, no property mapping, no discovery doc is
created for `gdx-mcp`.

### Why `gdx-mcp` is PAT-only, not an Authentik provider

- **Audit finding M-1 (v3 patch P19).** An earlier SS-6 draft defined a
  `gdx-mcp` OAuth provider with internally-contradictory grants — the
  provider table advertised `client_credentials`, but the config snippet
  used PKCE + `response_types=["code"]`. These are mutually exclusive
  OAuth flows, and neither fit the MCP usage pattern.
- **MCP auth model.** MCP clients (Claude Desktop, internal MCP callers)
  authenticate as a *user* with a long-lived bearer token that the user
  issues to themselves. That is a PAT, not an OAuth flow, and it does
  not need Authentik to mint or rotate it.
- **Where MCP auth actually lives:**
  - **SS-14** — PAT issuance, storage, scoping, revocation.
  - **SS-19** — MCP server bearer-token validation against the PAT
    store.
- **Security posture unchanged.** Removing the provider does not weaken
  auth; MCP tokens still validate against the platform, still carry
  tenant scope, and still fail closed. They just bypass Authentik
  entirely.

### Operator impact: do **not** run `configure_authentik.py --provider gdx-mcp`

This is enforced by code as well as documentation:

- `gdx_dispatch/tools/configure_authentik.py` uses `argparse` with
  `choices=("gdx-spa", "gdx-thirdparty")` after Slice C. Passing
  `--provider gdx-mcp` exits non-zero at parse time with an argparse
  error — **no network side effect is possible**.
- If future work needs to widen `--provider`, the allowlist MUST NOT
  include `gdx-mcp`. Anyone tempted to add it should read SS-14 first
  and re-read this section.

```bash
# This SHOULD fail at argparse time. If it ever succeeds, that is a bug.
python gdx_dispatch/tools/configure_authentik.py --provider gdx-mcp
# expected:
#   error: argument --provider: invalid choice: 'gdx-mcp' (choose from gdx-spa, gdx-thirdparty)
```

### What Slice B landed

- Reconciled `plans/platform-sprints/SS-6_authentik_provider_config.md`
  so the goal, provider table, configuration snippet, verification
  loops, acceptance criteria, and "What this SS does NOT do" section
  all describe a 2-provider world (`gdx-spa`, `gdx-thirdparty`).
- Added this disposition section + the top-of-file operator warning.
- Updated `ai-queue/claude_to_codex/current_result.md` with the
  reconciliation audit trail (before/after evidence, `git show
  --name-only HEAD`, scope guards).

### What Slice B did NOT do

- No product-code changes (no edits under `gdx_dispatch/tools/` or
  `gdx_dispatch/tests/`).
- No SS-14 implementation (PAT model is SS-14's bounded scope).
- No SS-19 implementation (MCP server bearer validation is SS-19's
  bounded scope).
- No `gdx-thirdparty` provider work (SS-6 Slice C).

## gdx-thirdparty provider (SS-6 Slice C)

SS-6 Slice C adds the `gdx-thirdparty` Authentik OAuth2 provider that
serves Zapier-class integrations planned under SS-21. It reuses the
shared `gdx_tid` scope mapping from Slice A but runs on its own signing
key, Application, and redirect URI so third-party token issuance stays
isolated from the SPA path (rotating the third-party key must not
invalidate SPA-issued tokens).

### Contract

| Field                     | Value                                                 |
|---------------------------|-------------------------------------------------------|
| Provider name             | `gdx-thirdparty`                                      |
| Audience (`aud` claim)    | `gdx-api`                                             |
| Access token TTL          | 1 hour (`hours=1`)                                    |
| Refresh token TTL         | 90 days (`days=90`)                                   |
| Authorization code TTL    | 1 minute (`minutes=1`)                                |
| Grant flow                | OAuth 2.1 authorization code (`response_types=["code"]`) |
| PKCE                      | **Required** (`pkce_mode="required"`, S256)           |
| Client type               | `confidential`                                        |
| Client ID                 | `gdx-thirdparty`                                      |
| Signing key               | `gdx-thirdparty-signing-key` (distinct from SPA key)  |
| Redirect URI (fixed)      | `https://integrations.example.com/oauth/callback` |
| Application slug / name   | `gdx-thirdparty` / `app-gdx-thirdparty`               |
| Shared scope mapping      | `gdx_tid` (same mapping as `gdx-spa`; emits singular active-tenant claim) |

The redirect URI is a **fixed** HTTPS URL on a GDX-owned host — no
wildcards (SS-6 audit P16). SS-21 integration contracts that need a
different redirect must land as a separate bounded slice that updates
`GDX_THIRDPARTY_REDIRECT_URI`; redirect list widening without that
review is out of scope.

### Verification

After running `configure_authentik.py --provider gdx-thirdparty`:

```bash
# Discovery + JWKS
curl -s https://auth.example.com/application/o/gdx-thirdparty/.well-known/openid-configuration | jq .
curl -s https://auth.example.com/application/o/gdx-thirdparty/jwks/ | jq '.keys[0]'

# Dry-run payload preview (no API call)
python gdx_dispatch/tools/configure_authentik.py --provider gdx-thirdparty --dry-run
```

Admin UI checks:

- Applications & Sources → Providers → `gdx-thirdparty` exists (in
  addition to `gdx-spa`).
- Crypto → Certificates → both `gdx-spa-signing-key` and
  `gdx-thirdparty-signing-key` are present (distinct keypairs).
- Applications → `app-gdx-thirdparty` slug resolves to the
  `gdx-thirdparty` provider.
- `gdx-mcp` provider does NOT exist (still PAT-only — see
  [MCP Disposition](#mcp-disposition-ss-6-slice-b)).

### What Slice C landed

- `gdx_dispatch/tools/configure_authentik.py`: added
  `build_gdx_thirdparty_provider_payload`, `configure_gdx_thirdparty`,
  `GDX_THIRDPARTY_*` constants, and widened argparse
  `choices=("gdx-spa", "gdx-thirdparty")`. SPA behavior preserved.
- `gdx_dispatch/tests/test_authentik_token_shape.py`: added deterministic
  provider-payload and synthesized-access-token tests for
  `gdx-thirdparty` (PKCE required, `response_types=["code"]`, fixed
  redirect, `aud=gdx-api`, 1-hour access / 90-day refresh,
  client_id/signing_key/redirect distinct from SPA). Existing
  `gdx-spa` assertions unchanged.
- `gdx_dispatch/docs/authentik_provider_config.md`: this section + the provider
  table / CLI / verification updates above.

### What Slice C did NOT do

- No `gdx-mcp` provider (still PAT-only — SS-14 / SS-19).
- No live Authentik API calls (dry-run / unit tests only).
- No JWT validation in GDX (SS-7).
- No SS-21 Zapier-class integration implementation (that slice will
  register its concrete redirect URI, scopes, and per-integration
  app).

## What Slice A does NOT do

- No live Authentik API integration tests (these require staging
  credentials; the token-shape tests validate the mapping and payload
  contract without hitting the network).
- No JWT validation in GDX (that lands in SS-7).
- No BFF `/auth/login` flow (SS-12).
