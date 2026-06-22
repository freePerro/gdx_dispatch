CREATE TABLE public.capabilities (
    id uuid NOT NULL,
    capability_set_id uuid NOT NULL,
    action character varying(32) NOT NULL,
    resource_type character varying(64) NOT NULL,
    instance_pattern character varying(255) DEFAULT '*'::character varying NOT NULL,
    conditions jsonb DEFAULT '{}'::jsonb NOT NULL,
    parent_capability_id uuid,
    granted_via_installation_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone
);
CREATE TABLE public.capability_sets (
    id uuid NOT NULL,
    name character varying(128) NOT NULL,
    description text,
    scope_type character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.game_definitions (
    id uuid NOT NULL,
    slug character varying(100) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    icon character varying(50),
    actor_type character varying(50) DEFAULT 'claude'::character varying NOT NULL,
    publisher character varying(100) DEFAULT 'system'::character varying NOT NULL,
    layout_json json DEFAULT '{}'::json NOT NULL,
    rules_json json DEFAULT '{}'::json NOT NULL,
    is_published boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    version integer DEFAULT 1 NOT NULL,
    created_by character varying(100) DEFAULT 'system'::character varying NOT NULL,
    tenant_id uuid
);
CREATE TABLE public.game_events (
    id uuid NOT NULL,
    actor_id character varying(100) NOT NULL,
    game_slug character varying(100) NOT NULL,
    event_type character varying(50) NOT NULL,
    value integer,
    value_string character varying(500),
    reason character varying(2000),
    created_by_user_id character varying(100),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.game_state (
    id uuid NOT NULL,
    actor_id character varying(100) NOT NULL,
    game_slug character varying(100) NOT NULL,
    lives integer DEFAULT 5 NOT NULL,
    max_lives integer DEFAULT 5 NOT NULL,
    hp integer DEFAULT 5 NOT NULL,
    max_hp integer DEFAULT 5 NOT NULL,
    xp integer DEFAULT 0 NOT NULL,
    current_phase character varying(100),
    state_json json DEFAULT '{}'::json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid
);
CREATE TABLE public.identities (
    id uuid NOT NULL,
    email character varying(255) NOT NULL,
    display_name character varying(255),
    status character varying(32) DEFAULT 'active'::character varying NOT NULL,
    email_verified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);
CREATE TABLE public.identity_providers (
    id uuid NOT NULL,
    identity_id uuid NOT NULL,
    provider_type character varying(32) NOT NULL,
    provider_subject character varying(255) NOT NULL,
    provider_email character varying(255),
    email_verified_by_provider boolean DEFAULT false NOT NULL,
    is_authoritative_for_domain boolean DEFAULT false NOT NULL,
    linked_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);
CREATE TABLE public.installations (
    id uuid NOT NULL,
    oauth_client_id uuid,
    installer_identity_id uuid NOT NULL,
    capability_set_id uuid NOT NULL,
    billing_account_id uuid,
    status character varying(32) DEFAULT 'active'::character varying NOT NULL,
    installed_at timestamp with time zone DEFAULT now() NOT NULL,
    uninstalled_at timestamp with time zone,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    health_status character varying(32) DEFAULT 'healthy'::character varying NOT NULL,
    last_event_at timestamp with time zone,
    tenant_id uuid NOT NULL
);
CREATE TABLE public.memberships (
    id uuid NOT NULL,
    identity_id uuid NOT NULL,
    role character varying(32) NOT NULL,
    capability_set_id uuid NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    granted_by_identity_id uuid,
    revoked_at timestamp with time zone,
    tenant_id uuid NOT NULL
);
CREATE TABLE public.platform_feature_flags (
    id uuid NOT NULL,
    flag_key character varying(100) NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    description character varying(500),
    tenant_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.server_errors (
    id uuid NOT NULL,
    tenant_id uuid,
    request_id character varying(64),
    method character varying(10),
    path text,
    status_code integer,
    exception_class character varying(200),
    exception_message text,
    traceback text,
    user_id character varying(64),
    user_email character varying(254),
    query_string text,
    referer text,
    user_agent text,
    git_sha character varying(40),
    group_fingerprint character varying(64),
    occurred_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    resolved_at timestamp with time zone,
    resolved_by character varying(64),
    resolution_note text
);
CREATE TABLE public.service_accounts (
    id uuid NOT NULL,
    name character varying(100) NOT NULL,
    description text,
    key_hash character varying(64) NOT NULL,
    key_prefix character varying(16) NOT NULL,
    allowed_scopes json DEFAULT '[]'::json NOT NULL,
    created_by character varying(255) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    allowed_tenant_uuids json
);
CREATE TABLE public.tenant_module_grants (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    module_key character varying(100) NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    granted_by character varying(200)
);
CREATE TABLE public.tenant_settings (
    tenant_id uuid NOT NULL,
    llm_provider_key_enc text,
    llm_provider_key_set_at timestamp with time zone,
    llm_provider_key_last_validated_at timestamp with time zone,
    llm_provider_key_last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    phone_com_token_enc text,
    phone_com_token_set_at timestamp with time zone,
    phone_com_token_last_validated_at timestamp with time zone,
    phone_com_token_last_error text,
    phone_com_webhook_secret text,
    outlook_microsoft_tenant_id character varying(64),
    outlook_client_id character varying(128),
    outlook_client_secret_enc text,
    outlook_secret_set_at timestamp with time zone,
    phone_com_webhook_callback_id bigint,
    phone_com_webhook_listener_id bigint,
    estimate_draft_archive_days integer DEFAULT 60 NOT NULL,
    job_number_format character varying(200) DEFAULT 'JOB-{year}-{seq:03d}'::character varying NOT NULL,
    job_number_next_seq integer DEFAULT 1 NOT NULL,
    job_number_year_seen integer,
    workflow_lock_schedule_on_start boolean DEFAULT false NOT NULL,
    workflow_post_arrival_event boolean DEFAULT false NOT NULL,
    workflow_sms_arrival_notify boolean DEFAULT false NOT NULL,
    workflow_require_parts_on_complete boolean DEFAULT true NOT NULL,
    workflow_require_hours_on_complete boolean DEFAULT true NOT NULL,
    workflow_require_signature_on_complete boolean DEFAULT true NOT NULL,
    default_payment_terms_days integer DEFAULT 30 NOT NULL,
    contractor_payment_terms_days integer,
    retail_payment_terms_days integer,
    wholesale_payment_terms_days integer,
    early_pay_discount_percent numeric(5,4),
    early_pay_discount_days integer,
    late_fee_flat_amount numeric(10,2),
    late_fee_percent numeric(5,4),
    late_fee_grace_days integer DEFAULT 0 NOT NULL,
    interest_rate_monthly_percent numeric(5,4),
    interest_grace_days integer DEFAULT 0 NOT NULL,
    catalog_require_description boolean DEFAULT false NOT NULL,
    catalog_render_name_when_desc_empty boolean DEFAULT true NOT NULL,
    catalog_ai_suggest_descriptions boolean DEFAULT false NOT NULL,
    catalog_block_zero_price_on_invoice boolean DEFAULT false NOT NULL,
    catalog_warn_zero_price_on_invoice boolean DEFAULT true NOT NULL,
    catalog_block_zero_price_on_save boolean DEFAULT false NOT NULL,
    catalog_auto_inactivate_zero_price boolean DEFAULT false NOT NULL,
    payroll_source character varying(40) DEFAULT 'manual'::character varying NOT NULL,
    maps_provider character varying(40) DEFAULT 'google_maps'::character varying NOT NULL,
    estimates_allow_line_margin_override boolean DEFAULT true NOT NULL,
    estimates_default_terms text,
    dispatch_warn_save_no_tech boolean DEFAULT false NOT NULL,
    dispatch_block_save_no_tech boolean DEFAULT false NOT NULL,
    dispatch_show_unassigned_lane boolean DEFAULT true NOT NULL,
    phone_com_webhook_secret_prev text,
    phone_com_webhook_secret_prev_until timestamp with time zone,
    phone_com_webhook_rotated_at timestamp with time zone,
    estimate_email_subject_template text,
    estimate_email_body_template text,
    estimate_deposit_pct integer DEFAULT 50 NOT NULL
);
CREATE TABLE public.tenants (
    id uuid NOT NULL,
    slug character varying(100) NOT NULL,
    name character varying(255) NOT NULL,
    subscription_status character varying(20) DEFAULT 'trialing'::character varying NOT NULL,
    timezone character varying(60) DEFAULT 'America/New_York'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    stripe_connect_account_id character varying(100),
    last_active_at timestamp with time zone,
    street character varying(255),
    city character varying(120),
    state character varying(80),
    postal_code character varying(20),
    country character varying(2) DEFAULT 'US'::character varying,
    phone character varying(32),
    employee_count integer,
    industry character varying(64)
);
ALTER TABLE ONLY public.capabilities
    ADD CONSTRAINT capabilities_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.capability_sets
    ADD CONSTRAINT capability_sets_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.game_definitions
    ADD CONSTRAINT game_definitions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.game_definitions
    ADD CONSTRAINT game_definitions_slug_key UNIQUE (slug);
ALTER TABLE ONLY public.game_events
    ADD CONSTRAINT game_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.game_state
    ADD CONSTRAINT game_state_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.identities
    ADD CONSTRAINT identities_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.identity_providers
    ADD CONSTRAINT identity_providers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.installations
    ADD CONSTRAINT installations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.platform_feature_flags
    ADD CONSTRAINT platform_feature_flags_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.server_errors
    ADD CONSTRAINT server_errors_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.service_accounts
    ADD CONSTRAINT service_accounts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tenant_module_grants
    ADD CONSTRAINT tenant_module_grants_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tenant_settings
    ADD CONSTRAINT tenant_settings_pkey PRIMARY KEY (tenant_id);
ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_slug_key UNIQUE (slug);
ALTER TABLE ONLY public.capability_sets
    ADD CONSTRAINT uq_capset_name_scope UNIQUE (name, scope_type);
ALTER TABLE ONLY public.platform_feature_flags
    ADD CONSTRAINT uq_feature_flag_key_tenant UNIQUE (flag_key, tenant_id);
ALTER TABLE ONLY public.game_state
    ADD CONSTRAINT uq_game_state_actor_game UNIQUE (actor_id, game_slug);
ALTER TABLE ONLY public.identity_providers
    ADD CONSTRAINT uq_idp_provider_subject UNIQUE (provider_type, provider_subject);
ALTER TABLE ONLY public.service_accounts
    ADD CONSTRAINT uq_service_accounts_key_hash UNIQUE (key_hash);
ALTER TABLE ONLY public.service_accounts
    ADD CONSTRAINT uq_service_accounts_name UNIQUE (name);
CREATE INDEX ix_capabilities_active ON public.capabilities USING btree (capability_set_id, resource_type, action) WHERE (revoked_at IS NULL);
CREATE INDEX ix_capabilities_capset ON public.capabilities USING btree (capability_set_id);
CREATE INDEX ix_capabilities_parent ON public.capabilities USING btree (parent_capability_id);
CREATE INDEX ix_capabilities_resource_type ON public.capabilities USING btree (resource_type);
CREATE INDEX ix_identities_email ON public.identities USING btree (email);
CREATE INDEX ix_identities_status ON public.identities USING btree (status);
CREATE INDEX ix_idp_identity_id ON public.identity_providers USING btree (identity_id);
CREATE INDEX ix_idp_provider_email ON public.identity_providers USING btree (provider_email);
CREATE INDEX ix_installations_status ON public.installations USING btree (status);
CREATE INDEX ix_memberships_identity ON public.memberships USING btree (identity_id);
CREATE INDEX ix_server_errors_exception_class ON public.server_errors USING btree (exception_class);
CREATE INDEX ix_server_errors_git_sha ON public.server_errors USING btree (git_sha);
CREATE INDEX ix_server_errors_group_fingerprint ON public.server_errors USING btree (group_fingerprint);
CREATE INDEX ix_server_errors_occurred_at ON public.server_errors USING btree (occurred_at);
CREATE INDEX ix_server_errors_open_recent ON public.server_errors USING btree (tenant_id, occurred_at) WHERE (resolved_at IS NULL);
CREATE INDEX ix_server_errors_path ON public.server_errors USING btree (path);
CREATE INDEX ix_server_errors_request_id ON public.server_errors USING btree (request_id);
CREATE INDEX ix_server_errors_status_code ON public.server_errors USING btree (status_code);
CREATE INDEX ix_server_errors_tenant_id ON public.server_errors USING btree (tenant_id);
CREATE INDEX ix_service_accounts_key_prefix ON public.service_accounts USING btree (key_prefix);
ALTER TABLE ONLY public.capabilities
    ADD CONSTRAINT capabilities_capability_set_id_fkey FOREIGN KEY (capability_set_id) REFERENCES public.capability_sets(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.capabilities
    ADD CONSTRAINT capabilities_parent_capability_id_fkey FOREIGN KEY (parent_capability_id) REFERENCES public.capabilities(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.capabilities
    ADD CONSTRAINT fk_capabilities_granted_via_install FOREIGN KEY (granted_via_installation_id) REFERENCES public.installations(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.game_definitions
    ADD CONSTRAINT fk_game_definitions_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.game_state
    ADD CONSTRAINT fk_game_state_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.installations
    ADD CONSTRAINT fk_installations_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT fk_memberships_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.platform_feature_flags
    ADD CONSTRAINT fk_platform_feature_flags_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.game_events
    ADD CONSTRAINT game_events_game_slug_fkey FOREIGN KEY (game_slug) REFERENCES public.game_definitions(slug) ON DELETE RESTRICT;
ALTER TABLE ONLY public.game_state
    ADD CONSTRAINT game_state_game_slug_fkey FOREIGN KEY (game_slug) REFERENCES public.game_definitions(slug) ON DELETE RESTRICT;
ALTER TABLE ONLY public.identity_providers
    ADD CONSTRAINT identity_providers_identity_id_fkey FOREIGN KEY (identity_id) REFERENCES public.identities(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.installations
    ADD CONSTRAINT installations_capability_set_id_fkey FOREIGN KEY (capability_set_id) REFERENCES public.capability_sets(id);
ALTER TABLE ONLY public.installations
    ADD CONSTRAINT installations_installer_identity_id_fkey FOREIGN KEY (installer_identity_id) REFERENCES public.identities(id);
ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_capability_set_id_fkey FOREIGN KEY (capability_set_id) REFERENCES public.capability_sets(id);
ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_granted_by_identity_id_fkey FOREIGN KEY (granted_by_identity_id) REFERENCES public.identities(id);
ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_identity_id_fkey FOREIGN KEY (identity_id) REFERENCES public.identities(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.tenant_module_grants
    ADD CONSTRAINT tenant_module_grants_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE ONLY public.tenant_settings
    ADD CONSTRAINT tenant_settings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;
CREATE OR REPLACE FUNCTION public.tenant_login_lookup(email_in text)
 RETURNS TABLE(identity_id uuid, tenant_id uuid, slug text, name text, role text, db_url_enc text)
 LANGUAGE sql SECURITY DEFINER SET search_path TO 'pg_catalog', 'public'
AS $function$
            SELECT i.id, t.id, t.slug, t.name, m.role, NULL::text
            FROM identities i
            JOIN memberships m ON m.identity_id = i.id
            JOIN tenants t ON t.id = m.tenant_id
            WHERE lower(i.email) = lower(email_in)
              AND i.deleted_at IS NULL AND m.revoked_at IS NULL AND t.deleted_at IS NULL
            ORDER BY t.slug;
        $function$;
