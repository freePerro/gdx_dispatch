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
ALTER TABLE ONLY public.game_definitions
    ADD CONSTRAINT game_definitions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.game_definitions
    ADD CONSTRAINT game_definitions_slug_key UNIQUE (slug);
ALTER TABLE ONLY public.game_events
    ADD CONSTRAINT game_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.game_state
    ADD CONSTRAINT game_state_pkey PRIMARY KEY (id);
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
ALTER TABLE ONLY public.platform_feature_flags
    ADD CONSTRAINT uq_feature_flag_key_tenant UNIQUE (flag_key, tenant_id);
ALTER TABLE ONLY public.game_state
    ADD CONSTRAINT uq_game_state_actor_game UNIQUE (actor_id, game_slug);
ALTER TABLE ONLY public.service_accounts
    ADD CONSTRAINT uq_service_accounts_key_hash UNIQUE (key_hash);
ALTER TABLE ONLY public.service_accounts
    ADD CONSTRAINT uq_service_accounts_name UNIQUE (name);
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
ALTER TABLE ONLY public.game_definitions
    ADD CONSTRAINT fk_game_definitions_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.game_state
    ADD CONSTRAINT fk_game_state_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.platform_feature_flags
    ADD CONSTRAINT fk_platform_feature_flags_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.game_events
    ADD CONSTRAINT game_events_game_slug_fkey FOREIGN KEY (game_slug) REFERENCES public.game_definitions(slug) ON DELETE RESTRICT;
ALTER TABLE ONLY public.game_state
    ADD CONSTRAINT game_state_game_slug_fkey FOREIGN KEY (game_slug) REFERENCES public.game_definitions(slug) ON DELETE RESTRICT;
ALTER TABLE ONLY public.tenant_module_grants
    ADD CONSTRAINT tenant_module_grants_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE ONLY public.tenant_settings
    ADD CONSTRAINT tenant_settings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;
