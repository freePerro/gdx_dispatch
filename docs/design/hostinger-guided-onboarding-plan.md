# Guided Onboarding Service — "Get Your Own GDX Dispatch Server"

**Status:** PARTIALLY BUILT (verified on main 2026-08-21). Build-order items
1 and 2 shipped: `gdx_dispatch/docker/docker-compose.customer.yml` and
`gdx_dispatch/tools/mint_runtime_env.py` (+ `tests/test_mint_runtime_env.py`).
**Not done:** items 3-8 — stage on our own VPS, the throwaway-VPS E2E, the
handover-guide template, the landing page with the Deploy button, the first
paying customer, and the Partner Program application.
**North star (Doug, 2026-08-17): ADOPTION, not revenue.** Monetization gets
figured out later; every decision below optimizes for the lowest-friction path
to a stranger running GDX Dispatch. Referral code stays on the button (it's a
*discount* for the customer — helps adoption and costs nothing).
**Model:** Customer owns everything — their Hostinger account, VPS, domain, and billing.
GDX sells the *setup service* (one-time fee + optional maintenance plan) and earns
Hostinger referral commissions. After handover we hold no credentials and no
infrastructure obligations.

## Why guided, not self-serve

Asking a garage-door company to run an installer is a support ticket with extra
steps. The customer's entire technical journey is: buy the VPS, fill one form,
approve access. Everything else is our standardized setup run.

## The funnel

1. **Landing page** on gdxdispatch.com with one button: **"Get Your Own GDX
   Dispatch Server"** — the official **Deploy on Hostinger button** (verified
   against Hostinger docs 2026-08-17):

   ```markdown
   [![Deploy on Hostinger](https://assets.hostinger.com/vps/deploy.svg)](https://www.hostinger.com/docker-hosting?compose_url=<COMPOSE_URL>&REFERRALCODE=<code>)
   ```

   Clicking it opens Hostinger's Docker Hosting checkout with our compose file
   preloaded; after purchase the VPS auto-prepares Docker and **Docker Manager
   opens ready to deploy**. The `REFERRALCODE` gives the customer an extra
   discount and us cashback — the button is the referral link. The same button
   goes in the GitHub README (AGPL self-hosters use the identical path).
   (The public demo moves to demo.gdxdispatch.com; the apex becomes the
   product/marketing site — also a Partner Program eligibility requirement.)
2. **Purchase + deploy** happen in one Hostinger flow, driven by the customer:
   checkout → Docker Manager opens preloaded → they fill the few required
   fields (see "first-boot self-configuration" below) → Deploy.
   - Recommended plan: **KVM 2** (2 vCPU / 8 GB / 100 GB) — $13.99/mo first
     term, $24.49 renewal (verified 2026-08-17). KVM 1 (4 GB) runs the stack
     but leaves little headroom.
   - Referral economics: 20% commission on the new purchase, 10% recurring on
     renewals (Partner Program; standard Referral Program covers day one while
     we're short of partner eligibility — currently 4 of 10 required hosted sites).
3. **Onboarding form** (short — linked from the landing page and the app's
   first-run screen; the guided *service* starts here, after their one-click
   deploy):
   - Business name (+ legal name for invoices/estimates branding)
   - Domain they want the app on (e.g. `dispatch.theircompany.com`); offer to
     register one via their Hostinger account if they have none. Their domain,
     never ours — preserves the walk-away property.
   - Hostinger account email (to receive the access request)
   - Preferred server region (informational if VPS already purchased; Hostinger
     picks datacenter at purchase — the purchase-step guide tells them what to
     choose; US customers → closest US datacenter)
   - Admin user email for the app
   - Which integrations they want wired now vs. later: Stripe, email
     (SMTP/Outlook), QuickBooks, Google Maps key, SMS provider, SimpleFIN/bank
     feeds. Each needs *their* credentials; the form links a one-page "what
     you'll need" checklist per integration.
4. **Access grant — never a password.** Two sanctioned paths, in order of preference:
   - **Hostinger Account Sharing / Agency Hub**: client grants our agency
     account collaborator access. (Open question: whether shared access covers
     API calls on their resources or panel-only — verify during first onboarding.)
   - **Scoped API token**: client creates an API token in their hPanel and
     pastes it into the form; we instruct them to revoke it at handover.
   - SSH during setup uses a keypair we generate per customer and remove at
     handover (or leave, labeled, if they buy the maintenance plan).
5. **Completion run** (our side, standardized — see runbook below). With the
   button flow the stack is already deployed by the customer; our service is
   everything around it: domain + DNS, TLS cutover, integrations, verification.
   Where a proper env block is needed, we redeploy the project in place
   (same `createNewProject` call) — data survives in named volumes.
6. **Handover guide** — one PDF/page: how to log into the app, how to log into
   hPanel, where billing lives (their Hostinger account), how to restart the
   server (hPanel reboot button), backup locations, how to request paid
   maintenance, and where the source lives (AGPL).

## Golden deployment package — exists vs. build

Substantially more of this exists than expected (audited 2026-08-17):

| Piece | Status |
|---|---|
| Published images | ✅ GHCR `gdx_dispatch` + `gdx_dispatch-plugin-host`, public, built by release.yml |
| Compose for customer boxes | ✅ `gdx_dispatch/docker/docker-compose.yml` + **`docker-compose.selfhost.yml`** overlay (pulls pinned `APP_VERSION`, single-migrator pattern, never builds on the box) |
| Updates | ✅ Docker Manager redeploy: re-calling `createNewProject` with the same name **replaces the project in place** — that IS the update path (one API call, or the customer clicks redeploy in hPanel). `update.sh` remains for our own prod |
| First-boot admin | ✅ entrypoint bootstrap seeds admin with `must_change_password=True` |
| Env template | ✅ `.env.template` — but 300 lines; needs a **customer-minimal subset** (~15 required keys) that the setup script fills from the form + generated secrets |
| DB backups | 🟡 `scripts/backup-db.sh` (cron pg_dump + 30-day prune) — hardcodes dev container name; parametrize. Hostinger weekly VPS snapshots supplement it |
| TLS / reverse proxy | ❌ **Biggest gap.** Prod sits behind Cloudflare; demo nginx conf is a reference only. Customer boxes need standalone auto-TLS → add a **Caddy** service to the selfhost overlay (auto Let's Encrypt for their domain, zero renewal ops) |
| Firewall | 🟡 Hostinger VPS firewall API (`createNewFirewall`/`createFirewallRule`/`activateFirewall`) — API calls against their VM, no ufw scripting, no SSH |
| Health/monitoring | 🟡 App has health + `/pwa/version` endpoints; `getProjectList`/`getProjectLogs` API shows container health remotely during setup. Our external uptime monitoring is a **paid maintenance feature**, not default — default model is walk-away |
| Deploy glue | ❌ Reduced to two artifacts: the **flattened single-file customer compose** (published per release) and a small **env-block generator** (form answers + minted secrets → the `environment` string) |

## Deployment mechanism: hPanel Docker Manager (adopted 2026-08-17)

Hostinger VPS has a native **Docker Manager**: compose projects deployed and
managed through hPanel, exposed via the API as "projects." Verified against the
API schema:

- **`VPS_createNewProjectV1(virtualMachineId, project_name, content, environment)`**
  — `content` is a URL to a compose file (or GitHub repo, auto-resolved) or raw
  YAML; `environment` is the env block. **One API call = full deployment.**
- Re-calling with the same `project_name` **replaces the project in place** —
  updates are the same single call (or the customer clicks redeploy in hPanel).
- `getProjectList` / `getProjectContainers` / `getProjectLogs` /
  `restartProject` / `stopProject` / `startProject` — remote visibility during
  setup, and the customer gets GUI buttons for all of it in their own hPanel.
- **No SSH, no server hardening, no update cron, no setup-customer.sh.**

This inverts the packaging principle: **everything ships inside the compose
file** — app + celery + plugin-host + postgres + redis, plus **Caddy**
(auto-TLS for their domain), plus a **backup sidecar** (nightly pg_dump to a
named volume, 30-day prune). The compose file *is* the product; any deploy
surface (Docker Manager, plain `docker compose`, a future one-click template)
gets it for free.

Mechanical note: Docker Manager wants ONE compose file. **Built 2026-08-17 as
a hand-authored standalone file** (`gdx_dispatch/docker/docker-compose.customer.yml`)
rather than CI-flattening base+overlay: the customer file differs structurally
(managed volumes instead of `external:` ones, no host-bound dev ports, added
caddy/backup/secrets-init services, curated env surface), so a flatten step
would have needed a bigger transform script than the file itself. release.yml
attaches it to every GitHub Release →
`…/releases/latest/download/docker-compose.customer.yml` is the stable
evergreen URL the button points at.

### First-boot self-configuration (required for the button flow)

The button means the *customer* deploys before we're ever involved, so the
stack must come up sane with near-zero env input:

- **Secrets are minted, not entered.** Built 2026-08-17 as a dedicated
  `secrets-init` service (app image, entrypoint override →
  `gdx_dispatch/tools/mint_runtime_env.py`, stdlib-only): runs before
  everything else, mints `SECRET_KEY`/`JWT_SECRET`/encryption keys/DB password
  and **persists them to the `gdx_secrets` volume** (`runtime.env` +
  `db_password` for `POSTGRES_PASSWORD_FILE`). Operator env wins and is folded
  into the file; the DB password is the one stored-wins key (postgres fixes it
  at first init). App/celery/plugin-host entrypoints load the file via
  `GDX_RUNTIME_ENV_FILE` (opt-in — prod/dev composes unaffected). Without
  persistence a container recreate rotates keys: sessions die and
  `EncryptedString` data becomes unreadable.
- **Human fields shrink to ~3**: business name, admin email, domain — entered
  in Docker Manager's env screen at deploy (all optional; defaults let the
  stack boot on `http://VPS_IP` and be configured later during our guided run).
- Caddy serves a self-signed/IP fallback until a domain is set; TLS activates
  when the domain env is applied (redeploy-in-place) and DNS points at the VM.

### Testing path (per Hostinger's own guidance)

Stage on our **existing VPS** first — Docker Manager → Compose, project name
`gdx-staging`, no production credentials: proves image pulls, boot ordering,
volume survival across replace, all without buying anything. ⚠ Prod + demo run
on that VM: the staging project must bind NO host ports that collide (no
80/443 — skip Caddy or bind alt ports) and should be sized-checked before
deploy, then torn down. The **throwaway VPS** is still the gate for the real
E2E: button → checkout → preloaded deploy → TLS on a test subdomain
(test.gdxdispatch.com) → restart/data-survival → handover walk.

## Setup runbook (per customer)

1. Preflight: DNS A record for their subdomain → their VPS IP (their DNS host,
   or Hostinger DNS API if the domain lives there).
2. Generate their env block (minted `SECRET_KEY`/`JWT_SECRET`/Fernet keys/DB
   password; `GDX_TENANT_NAME`, `GDX_PUBLIC_BASE_URL`, admin email from form;
   pinned `APP_VERSION`).
3. Firewall via API: 80/443 open, everything else closed.
4. `createNewProject` with the published compose URL + env block; watch
   `getProjectLogs` until healthy; capture seeded admin credential.
5. Wire requested integrations with the customer's credentials (screen-share —
   their keys never live in our systems).
6. Verify: browser walk of login → job → estimate → invoice on their URL;
   confirm first backup file exists.
7. Handover: guide + credentials, revoke our access (unless maintenance plan),
   log the install (version, domain, date).

Target: **≤ 30 minutes** of our time per customer.

No-access variant (maximum trust): because the compose URL is public, a
customer can paste URL + env block into Docker Manager themselves on a guided
call — we never touch their account at all. Default remains collaborator
access; this is the option for the security-conscious.

## Build order

1. ✅ **`docker-compose.customer.yml`** (2026-08-17, branch
   feat/deploy-on-hostinger): Caddy TLS + honest backup sidecar + healthcheck
   ordering + release.yml stable-URL publish. Local zero-env boot test passed
   (9/9 healthy, login 200, secrets survive force-recreate, old JWT valid).
2. ✅ **First-boot self-configuration** (same branch): `secrets-init` service +
   `gdx_dispatch/tools/mint_runtime_env.py` + entrypoint `GDX_RUNTIME_ENV_FILE`
   loading. Adversarial audit findings all fixed: plugin-host image got the
   entrypoint (was silently SQLite-split-brained), pwuser (uid 1001) can read
   runtime.env, operator env values validated (no shell/URL injection),
   backup can't report success on a failed pg_dump, celery sized for 8GB
   boxes. **Bycatch: fixed live prod session-death bug** — auth cookies
   hardcoded `domain=".example.com"` since the initial public release, so
   browsers rejected them and every session died at the 15-min access-token
   expiry; now host-only.
3. **Stage on our own VPS** via Docker Manager (`gdx-staging`, alt ports, no
   prod credentials, torn down after): pulls, boot ordering, volume survival
   across replace-in-place. ⚠ Needs a RELEASE cut first — the GHCR images
   must contain mint_runtime_env + both entrypoint changes.
4. **Prove the real E2E on a throwaway VPS** (~$10 one-month burn): button →
   checkout → preloaded deploy → TLS at test.gdxdispatch.com → backup restore.
5. **Handover guide template** + per-integration "what you'll need" checklists.
6. **Landing page with the Deploy button** + onboarding form on gdxdispatch.com
   (v0: Google Form) + the same button in the GitHub README.
7. **First paying customer**, timed; then consider deeper automation.
8. Partner Program application once eligibility is met (10+ hosted sites,
   agency site live).

## Endgame (later, if traction)

Hostinger's **VPS Application Catalog** is curated — there is no self-service
listing; inclusion requires Hostinger review. The Deploy button needs no
approval and is available now, so: button first, and separately prepare the
app for catalog review (clean compose, docs, public images — all produced by
build-order §1 anyway) once a handful of real installs prove it. A catalog
listing would move setup fully into VPS checkout, with the referral as the
entire funnel.

## Open questions

- Named-volume survival across Docker Manager project replace (build-order §2).
- Agency Hub shared access: panel-only or API-capable? (Decides whether setup
  runs on our partner access or a customer-minted API token.)
- Maintenance plan pricing/scope (updates-included vs. break-fix hourly).
- Support boundary wording on the landing page — integrations misconfigured by
  the customer are the likely ticket source, not hosting.
- Whether to offer a managed `{name}.gdxdispatch.com` subdomain for customers
  with no domain (convenient, but couples them to us — leaning no).
