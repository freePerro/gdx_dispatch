---
name: GDX UI login for Claude
description: Creds to log into gdx.example.com via the real login form (not JWT mint). Stored in .env as GDX_CLAUDE_EMAIL/GDX_CLAUDE_PASSWORD.
type: reference
originSessionId: 2dc50e8a-c153-48b5-9139-381da2f0f0e3
---
## How to log into GDX as Claude

**Account:** `auditor28@example.com` (admin role, GDX tenant `a1b2c3d4-e5f6-7890-abcd-ef1234567890`).

**Credentials:** in `/path/to/gdx_dispatch/.env` under `GDX_CLAUDE_EMAIL` and `GDX_CLAUDE_PASSWORD`. Never echo the password in transcripts.

**Password set:** 2026-04-21 an earlier session. Doug logged Claude in as admin, Claude used the `/users` UI Reset Password button to set a known password, then persisted to `.env`.

**Login endpoint:** `POST /auth/login` (NOT `/api/auth/login` — that returns 405). Returns `{access_token, token_type}`.

**Browser login:** navigate to `https://gdx.example.com/login`, fill email+password from `.env`. The SPA stores the token at `sessionStorage.gdx_access_token`.

**When to use this vs JWT mint (`reference_vps_ops.md`):**
- UI form login (this file): realistic user verification — exercises login form, password hashing, session cookie flow. Use when you want to confirm the whole login stack works for a real user.
- JWT mint: faster, no password required, useful when testing API-only or when auditor28's password is unknown/rotated.
