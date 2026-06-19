# Phone.com API (v4) Reference

Source: OpenAPI 3.0 spec pulled 2026-05-04 from ReadMe-hosted registry.
Spec also saved at `gdx_dispatch/docs/phonecom_openapi_v4.6.11.json` (638078 bytes).

- **Title:** Phoenix API by Phone.com
- **Version:** 4.6.11
- **Base URL:** `https://api.phone.com` (path prefix `/v4`)
- **Auth:** HTTP `bearer` token. Top-level `security` is unset, meaning each operation declares its own (most require bearer; `/v4/public/*`, `/swagger.yaml`, `/v4/ping`, and the `POST /v4/oauth/access-token` token-mint do not).
- **Account scoping:** Almost every path is `/v4/accounts/{voip_id}/...`. The numeric `voip_id` is the Phone.com account ID — get it from `GET /v4/accounts`. Many resources also have an extension-scoped twin under `/v4/accounts/{voip_id}/extensions/{extension_id}/...`.
- **Pagination:** standard `offset` / `limit` (max 500, default 25) + `sort[<field>]` and `filters[<field>]` query params on list endpoints.

## Auth flow

- Token-mint endpoint: `POST /v4/oauth/access-token` (unauthenticated). Supports four grants: Authorization Code, Client Credential, Password Credential, Refresh Token.
- OAuth client + redirect URIs are managed under `/v4/accounts/{voip_id}/oauth/clients` and `/v4/accounts/{voip_id}/oauth/clients/{oauth_client_id}/redirect-uris`.
- Permanent (non-expiring) tokens can be minted from the Phone.com Console API Client tool — typical pick for server-to-server.
- Token introspection: `GET /v4/oauth/access-token/details`. Revoke: `DELETE /v4/oauth/access-token`.
- Per-account/per-extension token listings live at `/v4/accounts/{voip_id}/oauth/access-tokens[/{id}]` and the extension twin.

## Webhook / event integration

Two layers ("events v.2"):

- **Listeners** — describe what events you care about. Filter rules: `/v4/accounts/{voip_id}/integrations/events/listeners/{listener_id}/filters`.
- **Subscriptions** — bind a listener to a delivery target (callback URL).
- **Callbacks** — the HTTP endpoint Phone.com POSTs events to. Health/stats per callback at `.../callbacks/{id}/health`.
- **Profiles** — saved listener+subscription bundles for reuse.
- **Overview** — `GET/POST /v4/accounts/{voip_id}/integrations/events/overview` returns the consolidated state and provisions a default integration.

All five resources have both account-scoped and extension-scoped variants. Legacy single-listener endpoints at `/v4/accounts/{voip_id}/listeners` still exist; prefer the `integrations/events/*` tree for new work.

## Endpoint catalog (172 operations, 66 tags)

Grouped by OpenAPI tag. `{voip_id}` = account ID, `{extension_id}` = extension ID.

### API Endpoint Statistics  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/api-statistics` | Shows API usage statistics |

### API Errors  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/api-errors` | List API Errors |

### Access Tokens  *(count: 6)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/oauth/access-tokens` | List Access Tokens for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/oauth/access-tokens/{id}` | Delete Access Token for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/oauth/access-tokens/{id}` | Get Access Token for extension |
| `GET` | `/v4/accounts/{voip_id}/oauth/access-tokens` | List Access Tokens |
| `DELETE` | `/v4/accounts/{voip_id}/oauth/access-tokens/{id}` | Delete Access Token |
| `GET` | `/v4/accounts/{voip_id}/oauth/access-tokens/{id}` | Get Access Token |

### Account Contacts  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/contacts` | List Contacts |
| `POST` | `/v4/accounts/{voip_id}/contacts` | Create Contact |

### Accounts  *(count: 3)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts` | List Accounts |
| `GET` | `/v4/accounts/{voip_id}` | Get Account |
| `PATCH` | `/v4/accounts/{voip_id}` | Update Account |

### Asynchronous Call Logs  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/async-call-logs` | Get List of Call Log Files |

### Available Phone Numbers  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/phone-numbers/available` | List Available Phone Numbers |
| `GET` | `/v4/public/phone-numbers/available` | List Available Phone Numbers (Public) |

### Blocked Calls  *(count: 8)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/blocked-calls` | List Blocked Calls |
| `POST` | `/v4/accounts/{voip_id}/blocked-calls` | Create Blocked Call |
| `DELETE` | `/v4/accounts/{voip_id}/blocked-calls/{id}` | Delete Blocked Call |
| `GET` | `/v4/accounts/{voip_id}/blocked-calls/{id}` | Get Blocked Call |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/blocked-calls` | List Blocked Call for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/blocked-calls` | Create Blocked Call for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/blocked-calls/{id}` | Delete Blocked Call for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/blocked-calls/{id}` | Get Blocked Call for extension |

### Call Logs  *(count: 6)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/call-logs` | List Call Logs |
| `GET` | `/v4/accounts/{voip_id}/call-logs/{id}` | Get Call Log |
| `GET` | `/v4/accounts/{voip_id}/call-logs/{id}/recording/download` | Download Call Log |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/call-logs` | List Call Logs for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/call-logs/{id}` | Get Call Log for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/call-logs/{id}/recording/download` | Download Call Log for extension |

### Call Reports  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/call-reports` | Get Call Report |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/call-reports` | Get Call Report for extension |

### Caller IDs  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/caller-ids` | List Caller IDs |

### Calls  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `POST` | `/v4/accounts/{voip_id}/calls` | Create Calls |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/calls` | Create calls for extension |

### Clients  *(count: 3)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/oauth/clients` | List OAuth Clients |
| `DELETE` | `/v4/accounts/{voip_id}/oauth/clients/{id}` | Delete OAuth Client |
| `GET` | `/v4/accounts/{voip_id}/oauth/clients/{id}` | Get OAuth Client |

### Conversations  *(count: 5)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/conversations` | List Conversations |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/conversations` | List Conversations for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/conversations/{id}` | Delete Conversation for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/conversations/{id}` | Get Conversation for extension |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/conversations/{id}` | Update Conversation for extension |

### Devices  *(count: 9)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/devices` | List Devices |
| `POST` | `/v4/accounts/{voip_id}/devices` | Create device |
| `DELETE` | `/v4/accounts/{voip_id}/devices/{id}` | Delete Device |
| `GET` | `/v4/accounts/{voip_id}/devices/{id}` | Get Device |
| `PATCH` | `/v4/accounts/{voip_id}/devices/{id}` | Patch Device |
| `PUT` | `/v4/accounts/{voip_id}/devices/{id}` | Replace Device |
| `GET` | `/v4/accounts/{voip_id}/devices/{id}/e911` | Get e911 address |
| `POST` | `/v4/accounts/{voip_id}/devices/{id}/e911` | Set new e911 address |
| `PUT` | `/v4/accounts/{voip_id}/devices/{id}/e911` | Sync e911 address |

### Events Callbacks  *(count: 10)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/callbacks` | Get list of callbacks for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/callbacks` | Create callback for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/callbacks/{id}` | Delete callback |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/callbacks/{id}` | Get callback for extension |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/callbacks/{id}` | Update callback for extension |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/callbacks` | Get list of callbacks |
| `POST` | `/v4/accounts/{voip_id}/integrations/events/callbacks` | Create callback |
| `DELETE` | `/v4/accounts/{voip_id}/integrations/events/callbacks/{id}` | Delete callback |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/callbacks/{id}` | Get callback |
| `PATCH` | `/v4/accounts/{voip_id}/integrations/events/callbacks/{id}` | Update callback |

### Events Callbacks Health Status  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/callbacks/{id}/health` | Get Statistics |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/callbacks/{callback_id}/health` | Get Statistics |

### Events Integrations Overview  *(count: 4)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/overview` | Events integration overview for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/overview` | Create Events Integration for extension |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/overview` | Events integration overview |
| `POST` | `/v4/accounts/{voip_id}/integrations/events/overview` | Create Events Integration |

### Events Listeners  *(count: 10)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/listeners` | Get list of listeners for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/listeners` | Creates listener for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/listeners/{id}` | Delete listener for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/listeners/{id}` | Get listener for extension |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/listeners/{id}` | Update listener for extension |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/listeners` | Get list of listeners |
| `POST` | `/v4/accounts/{voip_id}/integrations/events/listeners` | Create Listener |
| `DELETE` | `/v4/accounts/{voip_id}/integrations/events/listeners/{id}` | Delete listener |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/listeners/{id}` | Get listener |
| `PATCH` | `/v4/accounts/{voip_id}/integrations/events/listeners/{id}` | Update listener |

### Events Listeners Filters  *(count: 5)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/integrations/events/listeners/{listener_id}/filters` | Get list of listener's filters |
| `POST` | `/v4/accounts/{voip_id}/integrations/events/listeners/{listener_id}/filters` | Create listener's filter |
| `DELETE` | `/v4/accounts/{voip_id}/integrations/events/listeners/{listener_id}/filters/{id}` | Delete listener's filter |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/listeners/{listener_id}/filters/{id}` | Get listener's filter |
| `PATCH` | `/v4/accounts/{voip_id}/integrations/events/listeners/{listener_id}/filters/{id}` | Update listener's filter |

### Events Listeners Subscriptions  *(count: 9)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/listeners/{listener_id}/subscriptions` | Get list of subscriptions |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/listeners/{listener_id}/subscriptions` | Create subscription |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/listeners/{listener_id}/subscriptions/{id}` | Delete subscription |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/listeners/{listener_id}/subscriptions/{id}` | Get subscription |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/listeners/{listener_id}/subscriptions/{id}` | Update subscription |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/listeners/{listener_id}/subscriptions` | Get list of subscriptions |
| `POST` | `/v4/accounts/{voip_id}/integrations/events/listeners/{listener_id}/subscriptions` | Create subscription |
| `DELETE` | `/v4/accounts/{voip_id}/integrations/events/listeners/{listener_id}/subscriptions/{id}` | Delete subscription |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/listeners/{listener_id}/subscriptions/{id}` | Get subscription |

### Events Profiles  *(count: 10)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/profiles` | Get list of profiles |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/profiles` | Create profile |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/profiles/{id}` | Delete profile |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/profiles/{id}` | Get profile |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/integrations/events/profiles/{id}` | Update profile |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/profiles` | Get list of profiles |
| `POST` | `/v4/accounts/{voip_id}/integrations/events/profiles` | Create profile |
| `DELETE` | `/v4/accounts/{voip_id}/integrations/events/profiles/{id}` | Delete profile |
| `GET` | `/v4/accounts/{voip_id}/integrations/events/profiles/{id}` | Get profile |
| `PATCH` | `/v4/accounts/{voip_id}/integrations/events/profiles/{id}` | Update profile |

### Express Service Codes  *(count: 4)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/express-service-codes` | List Express Service Codes |
| `GET` | `/v4/accounts/{voip_id}/express-service-codes/{id}` | Get Express Service Codes |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/express-service-codes` | List Express Service Codes for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/express-service-codes/{id}` | Get Express Service Codes for extension |

### Extension Contact Groups  *(count: 5)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contact-groups` | List Groups |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contact-groups` | Create Group |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contact-groups/{id}` | Delete Group |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contact-groups/{id}` | Get Group |
| `PUT` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contact-groups/{id}` | Replace Group |

### Extension Contacts  *(count: 6)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contacts` | List Contacts |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contacts` | Create Contact |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contacts/{id}` | Delete Contact |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contacts/{id}` | Get Contact |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contacts/{id}` | Patch Contact |
| `PUT` | `/v4/accounts/{voip_id}/extensions/{extension_id}/contacts/{id}` | Replace Contact |

### Extensions  *(count: 7)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions` | List Extensions |
| `POST` | `/v4/accounts/{voip_id}/extensions` | Create Extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}` | Delete Extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}` | Get Extension |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}` | Patch Extension |
| `PUT` | `/v4/accounts/{voip_id}/extensions/{extension_id}` | Replace Extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/extensions` | List Extensions for extension |

### Fax  *(count: 12)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/fax` | List Fax for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/fax` | Create Fax for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/fax/{id}` | Delete Fax for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/fax/{id}` | Get Fax for extension |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/fax/{id}` | Patch Fax Status for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/fax/{id}/download` | Download Fax for extension |
| `GET` | `/v4/accounts/{voip_id}/fax` | List Fax |
| `POST` | `/v4/accounts/{voip_id}/fax` | Create Fax |
| `DELETE` | `/v4/accounts/{voip_id}/fax/{id}` | Delete Fax |
| `GET` | `/v4/accounts/{voip_id}/fax/{id}` | Get Fax |
| `PATCH` | `/v4/accounts/{voip_id}/fax/{id}` | Patch Fax Status |
| `GET` | `/v4/accounts/{voip_id}/fax/{id}/download` | Download Fax |

### Help Requests  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `POST` | `/v4/accounts/{voip_id}/help-requests` | Request help |

### Invoices  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/invoices` | List Invoices |
| `GET` | `/v4/accounts/{voip_id}/invoices/{id}` | Get Invoice |

### Listeners  *(count: 10)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/listeners` | List Listeners for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/listeners` | Create Listener for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/listeners/{id}` | Delete Listener for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/listeners/{id}` | Get Listener for extension |
| `PUT` | `/v4/accounts/{voip_id}/extensions/{extension_id}/listeners/{id}` | Replace Listener for extension |
| `GET` | `/v4/accounts/{voip_id}/listeners` | List Listeners |
| `POST` | `/v4/accounts/{voip_id}/listeners` | Create Listener |
| `DELETE` | `/v4/accounts/{voip_id}/listeners/{id}` | Delete Listener |
| `GET` | `/v4/accounts/{voip_id}/listeners/{id}` | Get Listener |
| `PUT` | `/v4/accounts/{voip_id}/listeners/{id}` | Replace Listener |

### Live Answer  *(count: 5)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/live-answer` | List Live Answer Scripts |
| `POST` | `/v4/accounts/{voip_id}/live-answer` | Create Live Answer Script |
| `DELETE` | `/v4/accounts/{voip_id}/live-answer/{id}` | Delete Live Answer |
| `GET` | `/v4/accounts/{voip_id}/live-answer/{id}` | Get Live Answer Script |
| `PATCH` | `/v4/accounts/{voip_id}/live-answer/{id}` | Patch Live Answer |

### Live Answer Usage  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/live-answer/{id}/usage` | List Live Answer Script Usage |

### Live Answer Vendor  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/live-answer-vendors` | List Live Answer Vendors |

### Live Answer Vendor Token  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `POST` | `/v4/accounts/{voip_id}/live-answer-vendors/{vendor_id}/tokens` | Create Live Answer Vendor Token |

### Media  *(count: 12)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/media` | List Media for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/media` | Create Media for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/media/{id}` | Delete Media for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/media/{id}` | Get Media for extension |
| `PUT` | `/v4/accounts/{voip_id}/extensions/{extension_id}/media/{id}` | Replace Media for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/media/{id}/download` | Download Media for extension |
| `GET` | `/v4/accounts/{voip_id}/media` | List Media |
| `POST` | `/v4/accounts/{voip_id}/media` | Create Media |
| `DELETE` | `/v4/accounts/{voip_id}/media/{id}` | Delete Media |
| `GET` | `/v4/accounts/{voip_id}/media/{id}` | Get Media |
| `PUT` | `/v4/accounts/{voip_id}/media/{id}` | Replace Media |
| `GET` | `/v4/accounts/{voip_id}/media/{id}/download` | Download Media |

### Media Usage  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/media/{id}/usage` | List Media Usage for extension |
| `GET` | `/v4/accounts/{voip_id}/media/{id}/usage` | List Media Usage |

### Menus  *(count: 5)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/menus` | List Menus |
| `POST` | `/v4/accounts/{voip_id}/menus` | Create Menu |
| `DELETE` | `/v4/accounts/{voip_id}/menus/{id}` | Delete Menu |
| `GET` | `/v4/accounts/{voip_id}/menus/{id}` | Get Menu |
| `PUT` | `/v4/accounts/{voip_id}/menus/{id}` | Replace Menu |

### Messages  *(count: 10)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/messages` | List Messages for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/messages` | Send Message for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/messages/{id}` | Delete Message for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/messages/{id}` | Get Message for extension |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/messages/{id}` | Update Message for extension |
| `GET` | `/v4/accounts/{voip_id}/messages` | List Messages |
| `POST` | `/v4/accounts/{voip_id}/messages` | Send Message |
| `DELETE` | `/v4/accounts/{voip_id}/messages/{id}` | Delete Message |
| `GET` | `/v4/accounts/{voip_id}/messages/{id}` | Get Message |
| `PATCH` | `/v4/accounts/{voip_id}/messages/{id}` | Update Message |

### OAuth  *(count: 5)*

| Method | Path | Summary |
|---|---|---|
| `DELETE` | `/v4/oauth/access-token` | Delete Access Token |
| `GET` | `/v4/oauth/access-token` | Get Access Token |
| `POST` | `/v4/oauth/access-token` | Create Access Token |
| `GET` | `/v4/oauth/access-token/details` | Detailed information about access token |
| `POST` | `/v4/oauth/access-token/notes` | Add note for token |

### Orders  *(count: 4)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/orders` | List Orders |
| `POST` | `/v4/accounts/{voip_id}/orders` | Create Order |
| `GET` | `/v4/accounts/{voip_id}/orders/{id}` | Get Order |
| `PATCH` | `/v4/accounts/{voip_id}/orders/{id}` | Update Order |

### Payment Methods  *(count: 5)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/payment-methods` | List Payment Methods |
| `POST` | `/v4/accounts/{voip_id}/payment-methods` | Create Payment Method |
| `DELETE` | `/v4/accounts/{voip_id}/payment-methods/{id}` | Delete Payment Method |
| `GET` | `/v4/accounts/{voip_id}/payment-methods/{id}` | Get Payment Method |
| `PATCH` | `/v4/accounts/{voip_id}/payment-methods/{id}` | Patch Payment Method |

### Payments  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/payments` | List payments |
| `POST` | `/v4/accounts/{voip_id}/payments` | Make payment |

### Phone Number Reservation  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `POST` | `/v4/accounts/{voip_id}/phone-numbers/reserved` | Reserve Phone Number |
| `POST` | `/v4/public/phone-numbers/reserved` | Reserve Phone Number (Public) |

### Phone Numbers  *(count: 8)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/phone-numbers` | List Phone Numbers for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/phone-numbers/{id}` | Get Phone Number for extension |
| `GET` | `/v4/accounts/{voip_id}/phone-numbers` | List Phone Numbers |
| `POST` | `/v4/accounts/{voip_id}/phone-numbers` | Create Phone Number |
| `DELETE` | `/v4/accounts/{voip_id}/phone-numbers/{id}` | Delete Phone Number |
| `GET` | `/v4/accounts/{voip_id}/phone-numbers/{id}` | Get Phone Number |
| `PATCH` | `/v4/accounts/{voip_id}/phone-numbers/{id}` | Patch Phone Number |
| `PUT` | `/v4/accounts/{voip_id}/phone-numbers/{id}` | Replace Phone Number |

### Phone Numbers Meta Information  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/phone-numbers/meta` | Get Phone Numbers Meta |

### Price List  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/devices-accessories-price-list` | Get Accessories Price List |
| `GET` | `/v4/devices-accessories-price-list` | Get Public Accessories Price List |

### Prices  *(count: 5)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/device-extensions-price-list` | Return device extensions price |
| `GET` | `/v4/accounts/{voip_id}/devices-price-list` | Get Devices Price List |
| `GET` | `/v4/accounts/{voip_id}/shipping-price-list` | Shipping Methods |
| `POST` | `/v4/accounts/{voip_id}/tax-calculations` | Calculate taxes |
| `GET` | `/v4/devices-price-list` | Get Public Devices Price List |

### Profile  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `POST` | `/v4/accounts/{voip_id}/profile/password` | Change password |

### Queue Members  *(count: 4)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/queues/{queue_id}/members` | List Queue Members |
| `POST` | `/v4/accounts/{voip_id}/queues/{queue_id}/members` | Create Queue Member |
| `DELETE` | `/v4/accounts/{voip_id}/queues/{queue_id}/members/{id}` | Delete Queue Member |
| `GET` | `/v4/accounts/{voip_id}/queues/{queue_id}/members/{id}` | Get Queue Member |

### Queues  *(count: 6)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/queues` | List Queues |
| `POST` | `/v4/accounts/{voip_id}/queues` | Create Queue |
| `DELETE` | `/v4/accounts/{voip_id}/queues/{id}` | Delete Queue |
| `GET` | `/v4/accounts/{voip_id}/queues/{id}` | Get Queue |
| `PATCH` | `/v4/accounts/{voip_id}/queues/{id}` | Patch Queue |
| `PUT` | `/v4/accounts/{voip_id}/queues/{id}` | Replace Queue |

### Redirect URIs  *(count: 4)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/oauth/clients/{oauth_client_id}/redirect-uris` | List OAuth Client Redirect URIs |
| `POST` | `/v4/accounts/{voip_id}/oauth/clients/{oauth_client_id}/redirect-uris` | Create OAuth Client Redirect URI |
| `DELETE` | `/v4/accounts/{voip_id}/oauth/clients/{oauth_client_id}/redirect-uris/{id}` | Delete OAuth Client Redirect URI |
| `GET` | `/v4/accounts/{voip_id}/oauth/clients/{oauth_client_id}/redirect-uris/{id}` | Get OAuth Client Redirect URI |

### Routes  *(count: 12)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/routes` | List Routes for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/routes` | Create Route for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/routes/{id}` | Delete Route for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/routes/{id}` | Get Route for extension |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/routes/{id}` | Patch Route |
| `PUT` | `/v4/accounts/{voip_id}/extensions/{extension_id}/routes/{id}` | Replace Route for extension |
| `GET` | `/v4/accounts/{voip_id}/routes` | List Routes |
| `POST` | `/v4/accounts/{voip_id}/routes` | Create Route |
| `DELETE` | `/v4/accounts/{voip_id}/routes/{id}` | Delete Route |
| `GET` | `/v4/accounts/{voip_id}/routes/{id}` | Get Route |
| `PATCH` | `/v4/accounts/{voip_id}/routes/{id}` | Patch Route |
| `PUT` | `/v4/accounts/{voip_id}/routes/{id}` | Replace Route |

### SMS  *(count: 12)*

| Method | Path | Summary |
|---|---|---|
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/sms` | Delete List SMS for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/sms` | List SMS for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/sms` | Create SMS for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/sms/{id}` | Delete SMS for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/sms/{id}` | Get SMS for extension |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/sms/{id}` | Patch SMS Status for extension |
| `DELETE` | `/v4/accounts/{voip_id}/sms` | Delete List SMS |
| `GET` | `/v4/accounts/{voip_id}/sms` | List SMS |
| `POST` | `/v4/accounts/{voip_id}/sms` | Create SMS |
| `DELETE` | `/v4/accounts/{voip_id}/sms/{id}` | Delete SMS |
| `GET` | `/v4/accounts/{voip_id}/sms/{id}` | Get SMS |
| `PATCH` | `/v4/accounts/{voip_id}/sms/{id}` | Patch SMS Status |

### Scheduled Requests  *(count: 8)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/scheduled-requests` | List Scheduled Requests for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/scheduled-requests` | Create Scheduled Request for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/scheduled-requests/{id}` | Delete Scheduled Request for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/scheduled-requests/{id}` | Get Scheduled Request for extension |
| `GET` | `/v4/accounts/{voip_id}/scheduled-requests` | List Scheduled Requests |
| `POST` | `/v4/accounts/{voip_id}/scheduled-requests` | Create Scheduled Request |
| `DELETE` | `/v4/accounts/{voip_id}/scheduled-requests/{id}` | Delete Scheduled Request |
| `GET` | `/v4/accounts/{voip_id}/scheduled-requests/{id}` | Get Scheduled Request |

### Schedules  *(count: 6)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/schedules` | List Schedules |
| `POST` | `/v4/accounts/{voip_id}/schedules` | Create Schedule |
| `DELETE` | `/v4/accounts/{voip_id}/schedules/{id}` | Delete Schedule |
| `GET` | `/v4/accounts/{voip_id}/schedules/{id}` | Get Schedule |
| `PATCH` | `/v4/accounts/{voip_id}/schedules/{id}` | Patch Schedule |
| `PUT` | `/v4/accounts/{voip_id}/schedules/{id}` | Replace Schedule |

### Subaccounts  *(count: 3)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/subaccounts` | List Subaccounts |
| `POST` | `/v4/accounts/{voip_id}/subaccounts` | Create Subaccount |
| `POST` | `/v4/accounts/{voip_id}/subaccounts/verification` | Request Verification Code |

### Support Requests  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/support-requests` | Submit Support Request for extension |
| `POST` | `/v4/accounts/{voip_id}/support-requests` | Submit Support Request |

### System endpoints  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/swagger.yaml` | Swagger file |
| `GET` | `/v4/ping` | Ping functionality |

### Transactions  *(count: 2)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/invoices/{invoice_id}/transactions` | List Transactions |
| `GET` | `/v4/accounts/{voip_id}/invoices/{invoice_id}/transactions/{id}` | Get Transaction |

### Trunks  *(count: 6)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/trunks` | List Trunks |
| `POST` | `/v4/accounts/{voip_id}/trunks` | Create Trunk |
| `DELETE` | `/v4/accounts/{voip_id}/trunks/{id}` | Delete Trunk |
| `GET` | `/v4/accounts/{voip_id}/trunks/{id}` | Get Trunk |
| `PATCH` | `/v4/accounts/{voip_id}/trunks/{id}` | Patch Trunk |
| `PUT` | `/v4/accounts/{voip_id}/trunks/{id}` | Replace Trunk |

### User  *(count: 9)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/users` | Get list of users |
| `POST` | `/v4/accounts/{voip_id}/users` | Create user |
| `GET` | `/v4/accounts/{voip_id}/users-statistics` | Get user statistics |
| `DELETE` | `/v4/accounts/{voip_id}/users/{id}` | Delete user |
| `GET` | `/v4/accounts/{voip_id}/users/{id}` | Get user |
| `PATCH` | `/v4/accounts/{voip_id}/users/{id}` | Update user |
| `POST` | `/v4/accounts/{voip_id}/users/{id}/avatar` | Set avatar |
| `POST` | `/v4/accounts/{voip_id}/users/{id}/invitations` | Invite |
| `POST` | `/v4/accounts/{voip_id}/users/{id}/password-reset-requests` | Reset passwrod |

### Video Conferences  *(count: 5)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/video-conferences` | List Conferences |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/video-conferences` | Schedule Conference |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/video-conferences/{id}` | Cancel Conference |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/video-conferences/{id}` | Get Conference |
| `PUT` | `/v4/accounts/{voip_id}/extensions/{extension_id}/video-conferences/{id}` | Replace Conference |

### Video Config  *(count: 3)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/video` | List Videos |
| `PATCH` | `/v4/accounts/{voip_id}/video` | Patch Video Status |
| `POST` | `/v4/accounts/{voip_id}/video` | Create Video |

### Video Token  *(count: 1)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/video/token` | Get Video Conference Token |

### Video Users  *(count: 6)*

| Method | Path | Summary |
|---|---|---|
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/video-user` | List User Videos for extension |
| `POST` | `/v4/accounts/{voip_id}/extensions/{extension_id}/video-user` | Create User Video for extension |
| `GET` | `/v4/accounts/{voip_id}/video/users` | List User Videos |
| `POST` | `/v4/accounts/{voip_id}/video/users` | Create User Video |
| `DELETE` | `/v4/accounts/{voip_id}/video/users/{id}` | Delete User Video |
| `GET` | `/v4/accounts/{voip_id}/video/users/{id}` | Get User Video |

### Voicemail  *(count: 12)*

| Method | Path | Summary |
|---|---|---|
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/voicemail` | Delete Voicemail (multiple) for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/voicemail` | List Voicemail for extension |
| `DELETE` | `/v4/accounts/{voip_id}/extensions/{extension_id}/voicemail/{id}` | Delete Voicemail for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/voicemail/{id}` | Get Voicemail for extension |
| `PATCH` | `/v4/accounts/{voip_id}/extensions/{extension_id}/voicemail/{id}` | Patch Voicemail Status for extension |
| `GET` | `/v4/accounts/{voip_id}/extensions/{extension_id}/voicemail/{id}/download` | Download Voicemail for extension |
| `DELETE` | `/v4/accounts/{voip_id}/voicemail` | Delete Voicemail (multiple) |
| `GET` | `/v4/accounts/{voip_id}/voicemail` | List Voicemail |
| `DELETE` | `/v4/accounts/{voip_id}/voicemail/{id}` | Delete Voicemail |
| `GET` | `/v4/accounts/{voip_id}/voicemail/{id}` | Get Voicemail |
| `PATCH` | `/v4/accounts/{voip_id}/voicemail/{id}` | Patch Voicemail Status |
| `GET` | `/v4/accounts/{voip_id}/voicemail/{id}/download` | Download Voicemail |

## Notes for GDX integration

- **Tenant identity rule still applies:** when we store a Phone.com `voip_id` against a GDX tenant, it goes in a control-plane row keyed by `tenant_id` (UUID). The `voip_id` is just a vendor identifier — never an FK target on our side.
- **Webhook receiver:** add a router under `gdx_dispatch/routers/integrations/phonecom.py` that verifies the bearer/HMAC Phone.com signs callbacks with (TBD — confirm signature scheme from `Events Callbacks` POST request schema before shipping).
- **Rate limits:** spec doesn't expose explicit limits; the docs site references an "API rate limits" page — ask support for written numbers before relying on a value in code.
- **Pagination defaults** (offset/limit, max 500) match what we already do in GDX list endpoints — wrap calls so the cursor pattern is consistent.
- **Twin endpoints:** account-scoped + extension-scoped pairs exist for almost every per-extension resource (sms, voicemail, fax, messages, conversations, etc.). Pick one consistently per call site.

## Source links

- API Documentation Center: https://apidocs.phone.com/docs
- API Reference index: https://apidocs.phone.com/reference
- Access Token guide: https://apidocs.phone.com/docs/access-token
- Create Access Token op: https://apidocs.phone.com/reference/post_v4-oauth-access-token
- Legacy reference site: https://docs.phone.com/refguides/refguideshome.html
