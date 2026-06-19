"""Maps provider selector — UX audit F-89 / 2026-04-29.

Per Doug 2026-04-29: "can we make maps a module that can be turned on
and off with 2 options google maps and mapbox or self-hosted osm, we
only need to do the google maps today. tenant provides their own keys."

Module on/off is the existing `maps` module key in MODULE_CATEGORIES —
tenants toggle visibility via /api/settings/modules. This sub-module
adds the provider *choice* (google_maps | mapbox | osm) inside the
Maps module. Today only google_maps is wired; the other options are
advertised so the Settings UI can show them as "planned."

Tenant-provided keys live where they already do:
  - app_settings.google_maps_api_key   (existing column, plain text —
                                        protected by HTTP-referrer
                                        restriction, not encryption)
  - future: mapbox_access_token, osm_tile_server_url
"""
