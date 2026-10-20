--
-- PostgreSQL database dump
--

\restrict gdxteststructure

-- Dumped from database version 16.15
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: automation_action_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.automation_action_type AS ENUM (
    'send_email',
    'send_sms',
    'create_task',
    'update_status',
    'wait'
);


--
-- Name: automation_enrollment_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.automation_enrollment_status AS ENUM (
    'active',
    'paused',
    'completed',
    'stopped'
);


--
-- Name: automation_trigger_event; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.automation_trigger_event AS ENUM (
    'job_completed',
    'estimate_sent',
    'invoice_overdue',
    'customer_created'
);


--
-- Name: equipment_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.equipment_type AS ENUM (
    'garage_door',
    'opener',
    'gate',
    'other'
);


--
-- Name: estimate_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.estimate_status AS ENUM (
    'draft',
    'sent',
    'accepted',
    'declined',
    'rejected',
    'expired'
);


--
-- Name: gl_account_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.gl_account_type AS ENUM (
    'asset',
    'liability',
    'equity',
    'revenue',
    'expense'
);


--
-- Name: gl_entry_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.gl_entry_status AS ENUM (
    'posted',
    'reversed'
);


--
-- Name: invoice_adjustment_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.invoice_adjustment_kind AS ENUM (
    'credit_memo',
    'refund',
    'credit_applied'
);


--
-- Name: invoice_billing_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.invoice_billing_type AS ENUM (
    'standard',
    'deposit',
    'progress',
    'final'
);


--
-- Name: invoice_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.invoice_status AS ENUM (
    'draft',
    'sent',
    'paid',
    'overdue',
    'void'
);


--
-- Name: job_billing_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.job_billing_status AS ENUM (
    'unbilled',
    'invoiced',
    'partial_paid',
    'paid',
    'overdue',
    'void'
);


--
-- Name: job_dispatch_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.job_dispatch_status AS ENUM (
    'unassigned',
    'assigned',
    'en_route',
    'on_site',
    'done'
);


--
-- Name: job_lifecycle_stage; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.job_lifecycle_stage AS ENUM (
    'lead',
    'service_call',
    'estimate',
    'scheduled',
    'in_progress',
    'completed',
    'cancelled'
);


--
-- Name: overhead_cadence; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.overhead_cadence AS ENUM (
    'weekly',
    'biweekly',
    'monthly',
    'quarterly',
    'semiannual',
    'annual'
);


--
-- Name: pricing_class; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.pricing_class AS ENUM (
    'retail',
    'contractor',
    'wholesale'
);


--
-- Name: proposal_tier_name; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.proposal_tier_name AS ENUM (
    'good',
    'better',
    'best'
);


--
-- Name: vehicle_service_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.vehicle_service_type AS ENUM (
    'oil_change',
    'tire_rotation',
    'inspection',
    'brake_service',
    'repair',
    'other'
);


--
-- Name: vehicle_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.vehicle_status AS ENUM (
    'available',
    'in_use',
    'maintenance',
    'retired'
);


--
-- Name: warranty_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.warranty_status AS ENUM (
    'active',
    'voided',
    'claimed',
    'expired'
);


--
-- Name: webhook_delivery_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.webhook_delivery_status AS ENUM (
    'pending',
    'delivered',
    'failed',
    'abandoned'
);


--
-- Name: workflow_run_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.workflow_run_status AS ENUM (
    'success',
    'failed',
    'skipped'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: ai_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_actions (
    id uuid NOT NULL,
    action_type character varying(100) NOT NULL,
    priority character varying(20) NOT NULL,
    payload json NOT NULL,
    status character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: ai_quote_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_quote_log (
    id character varying(36) NOT NULL,
    tenant_id text NOT NULL,
    job_type text NOT NULL,
    customer_id text,
    input_notes text,
    generated_quote json,
    accepted boolean,
    final_price numeric(12,2),
    feedback_notes text,
    feedback_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: ai_usage_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_usage_logs (
    id character varying(36) NOT NULL,
    tenant_id text NOT NULL,
    user_id text,
    task text NOT NULL,
    model text NOT NULL,
    input_tokens integer NOT NULL,
    output_tokens integer NOT NULL,
    total_tokens integer NOT NULL,
    cost_usd numeric(10,6) NOT NULL,
    latency_ms integer NOT NULL,
    request_id text,
    details text,
    created_at timestamp with time zone
);


--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_keys (
    id character(32) NOT NULL,
    tenant_id character(32) NOT NULL,
    key_hash character varying(64) NOT NULL,
    key_prefix character varying(16) NOT NULL,
    name character varying(100) NOT NULL,
    scopes json NOT NULL,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone
);


--
-- Name: app_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_settings (
    id uuid NOT NULL,
    company_name character varying(200) NOT NULL,
    address text NOT NULL,
    phone character varying(50) NOT NULL,
    email character varying(255) NOT NULL,
    logo character varying(500) NOT NULL,
    timezone character varying(100) NOT NULL,
    enabled_modules json NOT NULL,
    notification_preferences json NOT NULL,
    integrations json NOT NULL,
    primary_color character varying(20) NOT NULL,
    secondary_color character varying(20) NOT NULL,
    google_maps_api_key character varying(200),
    google_review_url character varying(500) DEFAULT ''::character varying NOT NULL,
    automation_emails_enabled boolean DEFAULT false NOT NULL,
    payment_plans_enabled boolean DEFAULT false NOT NULL,
    automation_sender_user_id character varying(36),
    on_my_way_sms_enabled boolean DEFAULT false NOT NULL,
    phone_com_voip_id character varying(40),
    phone_com_default_extension_id character varying(80),
    phone_com_default_caller_id character varying(40),
    phone_com_outbound_strategy character varying(40) DEFAULT 'tenant_default'::character varying NOT NULL,
    phone_com_account_features json,
    tenant_mobile_settings json DEFAULT '{}'::json NOT NULL,
    phone_com_last_synced_at timestamp with time zone,
    default_shift_start time without time zone DEFAULT '08:00:00'::time without time zone NOT NULL,
    default_shift_end time without time zone DEFAULT '17:00:00'::time without time zone NOT NULL,
    default_workdays smallint DEFAULT 31 NOT NULL,
    qb_accounting_method character varying(20) DEFAULT 'Accrual'::character varying NOT NULL,
    debug_logging_enabled boolean DEFAULT false NOT NULL,
    customer_listings_enabled boolean DEFAULT false NOT NULL,
    pay_period_cadence character varying(20) DEFAULT 'weekly_mon'::character varying NOT NULL,
    pay_period_anchor_start date,
    pay_period_pay_lag_days smallint DEFAULT 0 NOT NULL,
    payroll_recipient_emails text DEFAULT ''::text NOT NULL,
    payroll_autosend_enabled boolean DEFAULT false NOT NULL,
    payroll_autosend_hour smallint DEFAULT 7 NOT NULL,
    holiday_calendar json DEFAULT '[]'::json NOT NULL,
    time_off_default_minutes smallint DEFAULT 480 NOT NULL,
    time_off_counts_toward_overtime boolean DEFAULT false NOT NULL,
    vacation_allowance_hours numeric(6,2),
    vacation_year_policy character varying(20) DEFAULT 'calendar_year'::character varying NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: appointments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.appointments (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    job_id uuid,
    customer_id uuid,
    tech_id character varying(64),
    title character varying(300) NOT NULL,
    description text,
    address character varying(500),
    lat numeric(10,7),
    lng numeric(10,7),
    start_at timestamp with time zone NOT NULL,
    end_at timestamp with time zone NOT NULL,
    status character varying(20) DEFAULT 'scheduled'::character varying NOT NULL,
    confirmed_at timestamp with time zone,
    en_route_at timestamp with time zone,
    arrived_at timestamp with time zone,
    completed_at timestamp with time zone,
    day_closed_at timestamp with time zone,
    notes text,
    created_by character varying(200),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    customer_name character varying(200),
    duration_minutes integer,
    priority character varying(20),
    scheduled_end timestamp with time zone,
    scheduled_start timestamp with time zone,
    technician_id character varying(36)
);


--
-- Name: audit_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_logs (
    id uuid NOT NULL,
    tenant_id character varying(64),
    user_id character varying(64),
    action character varying(120) NOT NULL,
    entity_type character varying(80) NOT NULL,
    entity_id character varying(80),
    details json,
    ip_address character varying(64),
    request_id character varying(64),
    row_hash character varying(64) NOT NULL,
    prev_hash character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    event_type character varying(120),
    actor_id character varying(64),
    actor_role character varying(64),
    payload json,
    hash character varying(64)
);


--
-- Name: automation_enrollments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.automation_enrollments (
    id uuid NOT NULL,
    sequence_id uuid NOT NULL,
    entity_type character varying(50) NOT NULL,
    entity_id character varying(64) NOT NULL,
    status public.automation_enrollment_status NOT NULL,
    current_step integer NOT NULL,
    enrolled_at timestamp with time zone NOT NULL,
    next_run_at timestamp with time zone,
    paused_at timestamp with time zone,
    resumed_at timestamp with time zone,
    completed_at timestamp with time zone,
    stopped_at timestamp with time zone,
    stopped_reason character varying(120)
);


--
-- Name: automation_sequences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.automation_sequences (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    trigger_event public.automation_trigger_event NOT NULL,
    is_active boolean NOT NULL,
    is_paused boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: automation_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.automation_steps (
    id uuid NOT NULL,
    sequence_id uuid NOT NULL,
    step_order integer NOT NULL,
    action_type public.automation_action_type NOT NULL,
    delay_hours integer NOT NULL,
    template text NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: bank_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_accounts (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    kind character varying(20) NOT NULL,
    institution character varying(120) NOT NULL,
    last4 character varying(4) NOT NULL,
    statement_import_format character varying(30) DEFAULT 'pdf_community_bank'::character varying NOT NULL,
    gl_account_id uuid,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: bank_feed_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_feed_accounts (
    id uuid NOT NULL,
    connection_id uuid NOT NULL,
    provider character varying(20) DEFAULT 'banno'::character varying NOT NULL,
    external_account_id character varying(120) NOT NULL,
    name character varying(300),
    account_type character varying(50),
    account_subtype character varying(50),
    account_number_masked character varying(30),
    bank_account_id uuid,
    currency character varying(3) NOT NULL,
    balance numeric(14,2),
    available_balance numeric(14,2),
    balance_as_of timestamp with time zone,
    sync_enabled boolean NOT NULL,
    is_inactive boolean NOT NULL,
    initial_backfill_done boolean NOT NULL,
    backfill_started_at timestamp with time zone,
    backfill_synced_through date,
    updated_since_cursor timestamp with time zone,
    full_resync_required boolean NOT NULL,
    last_synced_at timestamp with time zone,
    raw_json json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: bank_feed_balance_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_feed_balance_snapshots (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    snapshot_date date NOT NULL,
    balance_cents bigint,
    available_balance_cents bigint,
    balance_as_of timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: bank_feed_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_feed_documents (
    id uuid NOT NULL,
    connection_id uuid NOT NULL,
    external_document_id character varying(120) NOT NULL,
    document_type character varying(20) NOT NULL,
    title character varying(300),
    filename character varying(200),
    document_date date,
    account_ids json,
    storage_path text,
    sha256 character varying(64),
    size_bytes bigint,
    content_type character varying(100),
    fetched_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: bank_feed_sync_schedule; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_feed_sync_schedule (
    id uuid NOT NULL,
    frequency character varying(20) DEFAULT 'manual'::character varying NOT NULL,
    backfill_days integer DEFAULT 365 NOT NULL,
    fetch_window_start character varying(5),
    fetch_window_end character varying(5),
    daily_fetch_cap integer DEFAULT 20 NOT NULL,
    fetch_count_today integer DEFAULT 0 NOT NULL,
    fetch_count_date date,
    last_run_at timestamp with time zone,
    next_run_at timestamp with time zone,
    last_run_status character varying(40),
    last_run_error text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: bank_feed_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_feed_transactions (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    external_transaction_id character varying(120) NOT NULL,
    amount_cents bigint,
    pending boolean NOT NULL,
    posted_at timestamp with time zone,
    posted_date date,
    payee character varying(300),
    memo text,
    filtered_memo text,
    check_number character varying(20),
    running_balance_cents bigint,
    txn_type character varying(50),
    txn_subtype character varying(50),
    merchant_name character varying(300),
    category character varying(120),
    line_hash character varying(64),
    external_last_updated timestamp with time zone,
    deleted_at timestamp with time zone,
    raw_json json,
    last_synced_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: bank_match_externals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_match_externals (
    id uuid NOT NULL,
    match_id uuid NOT NULL,
    source_table character varying(30) NOT NULL,
    source_id uuid NOT NULL,
    match_status character varying(12) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: bank_match_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_match_lines (
    id uuid NOT NULL,
    match_id uuid NOT NULL,
    line_id uuid NOT NULL,
    match_status character varying(12) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: bank_matches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_matches (
    id uuid NOT NULL,
    bank_account_id uuid NOT NULL,
    rule character varying(10) NOT NULL,
    status character varying(12) NOT NULL,
    classification character varying(20),
    confidence numeric(3,2),
    note text,
    created_by character varying(64),
    confirmed_by character varying(64),
    confirmed_at timestamp with time zone,
    created_expense_id uuid,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: bank_statement_imports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_statement_imports (
    id uuid NOT NULL,
    bank_account_id uuid NOT NULL,
    file_sha256 character varying(64) NOT NULL,
    original_filename character varying(255),
    storage_path text,
    period_start date NOT NULL,
    period_end date NOT NULL,
    beginning_balance_cents bigint NOT NULL,
    ending_balance_cents bigint NOT NULL,
    deposits_count integer,
    deposits_total_cents bigint NOT NULL,
    debits_count integer,
    debits_total_cents bigint NOT NULL,
    service_charge_cents bigint NOT NULL,
    interest_cents bigint NOT NULL,
    tie_out_status character varying(16) NOT NULL,
    tie_out_report json,
    lines_added integer NOT NULL,
    lines_deduped integer NOT NULL,
    voided_at timestamp with time zone,
    imported_by character varying(64),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: bank_statement_line_images; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_statement_line_images (
    id uuid NOT NULL,
    import_id uuid NOT NULL,
    line_id uuid,
    storage_path text NOT NULL,
    caption_check_no character varying(20),
    caption_amount_cents bigint,
    caption_date date,
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: bank_statement_line_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_statement_line_sources (
    id uuid NOT NULL,
    line_id uuid NOT NULL,
    import_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: bank_statement_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_statement_lines (
    id uuid NOT NULL,
    bank_account_id uuid NOT NULL,
    import_id uuid NOT NULL,
    txn_date date NOT NULL,
    amount_cents bigint NOT NULL,
    description text NOT NULL,
    section character varying(20) NOT NULL,
    check_number character varying(20),
    occurrence_n integer NOT NULL,
    line_hash character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: banno_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.banno_connections (
    id uuid NOT NULL,
    institution_id uuid NOT NULL,
    fi_host character varying(255) NOT NULL,
    banno_user_id character varying(64) NOT NULL,
    access_token_enc text,
    refresh_token_enc text,
    provider_base_url text,
    access_token_expires_at timestamp with time zone,
    refresh_token_expires_at timestamp with time zone,
    auth_state character varying(32) DEFAULT 'healthy'::character varying NOT NULL,
    documents_available boolean,
    documents_synced_through date,
    connected_by_user_id character varying(64),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: banno_institutions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.banno_institutions (
    id uuid NOT NULL,
    provider character varying(20) DEFAULT 'banno'::character varying NOT NULL,
    fi_host character varying(255) NOT NULL,
    display_label character varying(120) NOT NULL,
    client_id character varying(128),
    client_secret_enc text,
    secret_set_at timestamp with time zone,
    enabled boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: catalog_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.catalog_items (
    id uuid NOT NULL,
    wholesaler_tenant_id character varying(100) NOT NULL,
    sku character varying(100) NOT NULL,
    name character varying(200) NOT NULL,
    description character varying(500),
    base_price numeric(12,2) NOT NULL,
    metadata json,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: change_order_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.change_order_lines (
    id uuid NOT NULL,
    co_id uuid NOT NULL,
    description character varying(500) NOT NULL,
    qty integer NOT NULL,
    unit_price numeric(10,2) NOT NULL,
    line_total numeric(12,2) NOT NULL,
    taxable boolean DEFAULT true NOT NULL
);


--
-- Name: change_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.change_orders (
    id uuid NOT NULL,
    co_number character varying(50) NOT NULL,
    job_id uuid,
    customer_id uuid,
    customer_name character varying(200),
    title character varying(300) NOT NULL,
    description text,
    reason character varying(120),
    status character varying(30) NOT NULL,
    amount numeric(12,2) NOT NULL,
    approved_by character varying(200),
    approved_at timestamp with time zone,
    signature_url character varying(500),
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    customer_signature_token character varying(255),
    billed_invoice_id uuid,
    tax_rate numeric(9,6)
);


--
-- Name: channel_analytics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.channel_analytics (
    id uuid NOT NULL,
    wholesaler_tenant_id character varying(100) NOT NULL,
    period_start timestamp with time zone NOT NULL,
    period_end timestamp with time zone NOT NULL,
    active_distributors integer,
    total_channel_revenue numeric(14,2) NOT NULL,
    computed_at timestamp with time zone NOT NULL
);


--
-- Name: checklist_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklist_items (
    id text NOT NULL,
    tenant_id text NOT NULL,
    checklist_id text NOT NULL,
    item_label text NOT NULL,
    completed integer NOT NULL
);


--
-- Name: checklist_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklist_templates (
    id text NOT NULL,
    tenant_id text NOT NULL,
    name text NOT NULL,
    items_json text NOT NULL,
    created_at text NOT NULL
);


--
-- Name: checklists; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklists (
    id text NOT NULL,
    tenant_id text NOT NULL,
    job_id text NOT NULL,
    template_id text NOT NULL,
    created_at text NOT NULL
);


--
-- Name: chi_door_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chi_door_catalog (
    id uuid NOT NULL,
    sku character varying(255) NOT NULL,
    source_id integer,
    brand character varying(255),
    manufacturer character varying(255),
    model_number character varying(100),
    door_type character varying(100),
    description text,
    sales_talking_point text,
    width numeric(6,2),
    height numeric(6,2),
    color character varying(255),
    cost numeric(10,2),
    insulation_type character varying(100),
    r_value numeric(5,2),
    panel_style character varying(255),
    section_construction character varying(255),
    section_thickness_in numeric(5,2),
    section_sides integer,
    section_material character varying(255),
    window_option character varying(10),
    window_rows integer,
    window_type character varying(100),
    finish_type character varying(100),
    high_lift character varying(10),
    high_lift_in integer,
    is_custom boolean NOT NULL,
    web_source_url character varying(500),
    is_active boolean NOT NULL,
    imported_at timestamp with time zone NOT NULL,
    chi_order_number character varying(100),
    sell_price numeric(10,2),
    pricing_category character varying(40)
);


--
-- Name: chi_parts_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chi_parts_catalog (
    id uuid NOT NULL,
    sku character varying(255) NOT NULL,
    source_id integer,
    name character varying(500) NOT NULL,
    part_type character varying(100),
    brand character varying(255),
    manufacturer character varying(255),
    model character varying(255),
    cost numeric(10,2),
    description text,
    rail_length_ft integer,
    mount_type character varying(100),
    window_style character varying(100),
    window_inserts character varying(100),
    is_active boolean NOT NULL,
    imported_at timestamp with time zone NOT NULL,
    sell_price numeric(10,2),
    pricing_category character varying(40)
);


--
-- Name: client_errors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.client_errors (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    api_url text,
    method character varying(10),
    status_code integer,
    detail text,
    page_url text,
    created_at timestamp with time zone
);


--
-- Name: commission_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.commission_entries (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    job_id character varying(36) NOT NULL,
    parts_amount numeric(12,2) NOT NULL,
    labor_amount numeric(12,2) NOT NULL,
    bonus_amount numeric(12,2) NOT NULL,
    total numeric(12,2) NOT NULL,
    period character varying(10) NOT NULL,
    created_at timestamp with time zone
);


--
-- Name: commission_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.commission_rules (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    role character varying(100) NOT NULL,
    parts_pct numeric(5,2) NOT NULL,
    labor_pct numeric(5,2) NOT NULL,
    bonus_per_review numeric(10,2) NOT NULL,
    created_at timestamp with time zone,
    updated_at timestamp with time zone
);


--
-- Name: companies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.companies (
    id character varying(36) NOT NULL,
    name character varying(255),
    stripe_connect_account_id character varying(100),
    stripe_customer_id character varying(100),
    created_at timestamp with time zone
);


--
-- Name: company_module_grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company_module_grants (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    module_key character varying(100) NOT NULL,
    granted_at timestamp with time zone,
    created_at timestamp with time zone,
    expires_at timestamp with time zone
);


--
-- Name: custom_catalog_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.custom_catalog_items (
    id uuid NOT NULL,
    catalog_id uuid NOT NULL,
    sku character varying(100),
    name character varying(200) NOT NULL,
    description text,
    cost numeric(12,2) NOT NULL,
    price numeric(12,2) NOT NULL,
    category character varying(120),
    vendor character varying(200),
    pricing_category character varying(40),
    product_class character varying(40) DEFAULT 'parts'::character varying NOT NULL,
    attributes json DEFAULT '{}'::json NOT NULL,
    active boolean NOT NULL,
    qb_item_id character varying(120),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: custom_catalogs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.custom_catalogs (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    source_system character varying(60) NOT NULL,
    product_class character varying(40) DEFAULT 'parts'::character varying NOT NULL,
    field_schema json DEFAULT '[]'::json NOT NULL,
    pricing_strategy character varying(40) DEFAULT 'manual'::character varying NOT NULL,
    pricing_config json DEFAULT '{}'::json NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: custom_field_definitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.custom_field_definitions (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    entity_type character varying(30) NOT NULL,
    field_key character varying(80) NOT NULL,
    label character varying(200) NOT NULL,
    field_type character varying(30) NOT NULL,
    options text,
    required boolean NOT NULL,
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: custom_field_values; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.custom_field_values (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    definition_id uuid NOT NULL,
    entity_type character varying(30) NOT NULL,
    entity_id character varying(64) NOT NULL,
    value text,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: customer_contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customer_contacts (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    customer_id uuid NOT NULL,
    name text NOT NULL,
    phone text,
    email text,
    label character varying(120),
    is_primary boolean DEFAULT false NOT NULL,
    created_by character varying(36),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: customer_equipments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customer_equipments (
    id uuid NOT NULL,
    customer_id uuid NOT NULL,
    equipment_type public.equipment_type NOT NULL,
    manufacturer character varying(100),
    model character varying(100),
    serial_number character varying(100),
    installation_date date,
    last_service_date date,
    warranty_expires_on date,
    notes text,
    metadata json,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: customer_locations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customer_locations (
    id character varying(36) NOT NULL,
    customer_id character varying(36) NOT NULL,
    label character varying(200),
    address text,
    access_notes text,
    is_primary boolean,
    created_at timestamp with time zone,
    deleted_at timestamp with time zone,
    city character varying(120),
    company_id character varying(36) NOT NULL,
    lat numeric(10,7),
    lng numeric(10,7),
    state character varying(20),
    zip character varying(20),
    local_edit_at timestamp with time zone,
    local_edit_fields json
);


--
-- Name: customer_reviews; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customer_reviews (
    id text NOT NULL,
    tenant_id text,
    job_id text,
    customer_id text,
    token text,
    rating integer,
    review_text text,
    status text NOT NULL,
    sent_at text,
    submitted_at text,
    created_at text NOT NULL,
    company_id character varying(36) NOT NULL,
    deleted_at timestamp with time zone,
    google_reviews_link character varying(500),
    message text,
    scheduled_for timestamp with time zone,
    source character varying(100)
);


--
-- Name: customer_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customer_users (
    id uuid NOT NULL,
    customer_id uuid NOT NULL,
    email character varying(200) NOT NULL,
    password_hash character varying(200),
    is_active boolean NOT NULL,
    last_login_at timestamp with time zone,
    portal_token character varying(64),
    portal_token_expires_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: customer_volume_discount_tiers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customer_volume_discount_tiers (
    id uuid NOT NULL,
    settings_id uuid NOT NULL,
    volume_min_12mo numeric(14,2) NOT NULL,
    volume_max_12mo numeric(14,2),
    discount_pct numeric(6,4) NOT NULL,
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: customers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customers (
    id uuid NOT NULL,
    name text NOT NULL,
    name_hash character varying(64),
    email text,
    email_hash character varying(64),
    phone text,
    phone_hash character varying(64),
    address text,
    metadata json,
    notes text,
    source character varying(50),
    local_edit_at timestamp with time zone,
    local_edit_fields json,
    company_id character varying(36) NOT NULL,
    customer_type character varying(50),
    can_submit_listings boolean DEFAULT false NOT NULL,
    pricing_class public.pricing_class,
    margin_override_pct numeric(6,4),
    payment_terms_days integer,
    cached_rolling_volume_paid_12mo numeric(14,2),
    cached_rolling_volume_at timestamp with time zone,
    qb_dirty boolean DEFAULT true NOT NULL,
    qb_synced_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    email_opt_out boolean,
    sms_opt_out boolean,
    notes_appended text,
    version integer DEFAULT 1 NOT NULL
);


--
-- Name: dealer_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dealer_orders (
    id uuid NOT NULL,
    dealer_tenant_id character varying(100) NOT NULL,
    distributor_tenant_id character varying(100) NOT NULL,
    order_number character varying(50) NOT NULL,
    status character varying(30) NOT NULL,
    line_items json,
    total_amount numeric(12,2) NOT NULL,
    idempotency_key character varying(100),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: device_alert_registrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.device_alert_registrations (
    id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    token_hash character varying(64) NOT NULL,
    device_label character varying(200),
    created_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone,
    revoked_at timestamp with time zone,
    revoked_reason character varying(40)
);


--
-- Name: device_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.device_tokens (
    id uuid NOT NULL,
    tenant_id character varying(50) NOT NULL,
    user_id character varying(50) NOT NULL,
    platform character varying(20) NOT NULL,
    token character varying(500) NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: dispatch_routes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dispatch_routes (
    id uuid NOT NULL,
    technician_id character varying(50) NOT NULL,
    job_id uuid NOT NULL,
    estimated_arrival timestamp with time zone,
    distance_km numeric(8,2),
    created_at timestamp with time zone NOT NULL
);


--
-- Name: distributor_analytics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.distributor_analytics (
    id uuid NOT NULL,
    distributor_tenant_id character varying(100) NOT NULL,
    period_start timestamp with time zone NOT NULL,
    period_end timestamp with time zone NOT NULL,
    active_dealers integer,
    total_orders integer,
    total_revenue numeric(14,2) NOT NULL,
    computed_at timestamp with time zone NOT NULL
);


--
-- Name: document_folders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_folders (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    parent_id uuid,
    description text,
    created_by character varying(100),
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: document_signatures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_signatures (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    document_type character varying(30) NOT NULL,
    document_id character varying(64) NOT NULL,
    status character varying(20) NOT NULL,
    signature_data text,
    signed_by character varying(200),
    signed_by_email character varying(254),
    signed_at timestamp with time zone,
    signed_ip character varying(45),
    token character varying(64),
    token_expires_at timestamp with time zone,
    requested_by character varying(200),
    requested_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.documents (
    id uuid NOT NULL,
    filename character varying(255) NOT NULL,
    original_name character varying(255) NOT NULL,
    file_size integer NOT NULL,
    content_type character varying(150),
    uploaded_by character varying(100),
    title character varying(255),
    description text,
    folder_id uuid,
    job_id uuid,
    customer_id uuid,
    estimate_id uuid,
    tags character varying(500),
    content_hash character varying(64),
    uploaded_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone,
    size_bytes integer,
    tenant_id character varying(64),
    entity_type character varying(50),
    entity_id character varying(64)
);


--
-- Name: door_listing_photos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.door_listing_photos (
    id uuid NOT NULL,
    listing_id uuid NOT NULL,
    filename character varying(255) NOT NULL,
    content_type character varying(80) DEFAULT 'image/jpeg'::character varying NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: door_listings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.door_listings (
    id uuid NOT NULL,
    listing_type character varying(20) NOT NULL,
    title character varying(200) NOT NULL,
    slug character varying(220) NOT NULL,
    description text,
    status character varying(20) DEFAULT 'draft'::character varying NOT NULL,
    condition character varying(20),
    width_in numeric(6,2),
    height_in numeric(6,2),
    material character varying(60),
    color character varying(60),
    insulation_r numeric(5,2),
    has_windows boolean DEFAULT false NOT NULL,
    brand character varying(80),
    model character varying(80),
    qty integer DEFAULT 1 NOT NULL,
    featured boolean DEFAULT false NOT NULL,
    price numeric(10,2),
    compare_at_price numeric(10,2),
    price_display character varying(20) DEFAULT 'fixed'::character varying NOT NULL,
    source character varying(20) DEFAULT 'office'::character varying NOT NULL,
    submitted_by_user_id uuid,
    submitted_by_customer_id uuid,
    submitted_at timestamp with time zone,
    reviewed_by_user_id uuid,
    reviewed_at timestamp with time zone,
    rejection_reason text,
    source_job_id uuid,
    published_at timestamp with time zone,
    sold_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: door_specs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.door_specs (
    catalog_item_id uuid NOT NULL,
    manufacturer character varying(255),
    model_number character varying(100),
    door_type character varying(100),
    sales_talking_point text,
    width numeric(6,2),
    height numeric(6,2),
    color character varying(255),
    insulation_type character varying(100),
    r_value numeric(5,2),
    panel_style character varying(255),
    section_construction character varying(255),
    section_thickness_in numeric(5,2),
    section_sides integer,
    section_material character varying(255),
    window_option character varying(10),
    window_rows integer,
    window_type character varying(100),
    finish_type character varying(100),
    high_lift character varying(10),
    high_lift_in integer,
    web_source_url character varying(500),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: email_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_settings (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    provider character varying(20),
    smtp_host character varying(200),
    smtp_port integer,
    username character varying(200),
    password_enc text,
    from_email character varying(254),
    from_name character varying(100),
    reply_to_email character varying(254),
    is_verified boolean,
    created_at timestamp with time zone,
    updated_at timestamp with time zone
);


--
-- Name: equipment_asset_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.equipment_asset_history (
    id text NOT NULL,
    tenant_id text NOT NULL,
    equipment_id text NOT NULL,
    service_type text NOT NULL,
    service_date text NOT NULL,
    technician_id text NOT NULL,
    notes text
);


--
-- Name: equipment_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.equipment_assets (
    id text NOT NULL,
    tenant_id text NOT NULL,
    customer_id text NOT NULL,
    equipment_type text NOT NULL,
    manufacturer text,
    model text,
    serial_number text,
    warranty_expires_on text,
    install_date text,
    notes text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    deleted_at text
);


--
-- Name: equipment_service_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.equipment_service_history (
    id uuid NOT NULL,
    equipment_id uuid NOT NULL,
    job_id uuid,
    service_type character varying(100) NOT NULL,
    technician_id character varying(50) NOT NULL,
    service_date timestamp with time zone NOT NULL,
    notes text,
    parts_used json
);


--
-- Name: estimate_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.estimate_lines (
    id uuid NOT NULL,
    estimate_id uuid NOT NULL,
    description text NOT NULL,
    category character varying(80),
    quantity integer NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    line_total numeric(12,2) NOT NULL,
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    cost_snapshot numeric(12,2),
    margin_pct_snapshot numeric(6,4),
    margin_pct_override numeric(6,4),
    pricing_source character varying(32),
    labor_price_item_id uuid,
    estimated_man_hours numeric(6,2),
    line_metadata json,
    company_id character varying(36) NOT NULL
);


--
-- Name: estimate_nurture_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.estimate_nurture_log (
    id uuid NOT NULL,
    estimate_id character varying(36) NOT NULL,
    rule_id character varying(36) NOT NULL,
    sent_at timestamp with time zone NOT NULL,
    channel character varying(30)
);


--
-- Name: estimate_nurture_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.estimate_nurture_rules (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    delay_hours integer NOT NULL,
    message_template text,
    discount_pct numeric(5,2) NOT NULL,
    active boolean,
    created_at timestamp with time zone,
    updated_at timestamp with time zone
);


--
-- Name: estimates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.estimates (
    id uuid NOT NULL,
    job_id uuid,
    customer_id uuid,
    lead_id uuid,
    estimate_number character varying(50) NOT NULL,
    label character varying(200),
    jobsite_address text,
    description text,
    notes text,
    tax_rate numeric(9,6),
    discount numeric(12,2),
    proposal_mode boolean NOT NULL,
    hide_line_prices boolean,
    total numeric(12,2) NOT NULL,
    status public.estimate_status NOT NULL,
    sent_at timestamp with time zone,
    sent_via character varying(20),
    accepted_at timestamp with time zone,
    declined_at timestamp with time zone,
    declined_reason text,
    accepted_tier_id uuid,
    valid_until timestamp with time zone,
    signature_data text,
    signed_by character varying(200),
    signed_at timestamp with time zone,
    company_id character varying(36) NOT NULL,
    reminder_sent_at timestamp with time zone,
    public_token character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone,
    version integer DEFAULT 1 NOT NULL
);


--
-- Name: expense_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.expense_lines (
    id uuid NOT NULL,
    expense_id uuid NOT NULL,
    account character varying(120) NOT NULL,
    amount numeric(12,2) NOT NULL,
    description text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: expense_receipts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.expense_receipts (
    id uuid NOT NULL,
    expense_id uuid NOT NULL,
    filename character varying(200) NOT NULL,
    content_type character varying(100) NOT NULL,
    size_bytes bigint NOT NULL,
    sha256 character varying(64) NOT NULL,
    storage_path text NOT NULL,
    uploaded_by character varying(64),
    company_id character varying(36) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: expenses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.expenses (
    id uuid NOT NULL,
    vendor character varying(200) NOT NULL,
    amount numeric(12,2) NOT NULL,
    date date NOT NULL,
    category character varying(100) NOT NULL,
    description text,
    job_id uuid,
    status character varying(20) DEFAULT 'draft'::character varying NOT NULL,
    source character varying(20) DEFAULT 'manual'::character varying NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    company_id character varying(36) NOT NULL
);


--
-- Name: fleet_vehicle_service_logs_router; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fleet_vehicle_service_logs_router (
    id text NOT NULL,
    tenant_id text NOT NULL,
    vehicle_id text NOT NULL,
    service_type text NOT NULL,
    mileage_at_service integer NOT NULL,
    service_date text NOT NULL,
    notes text
);


--
-- Name: fleet_vehicles_router; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fleet_vehicles_router (
    id text NOT NULL,
    tenant_id text NOT NULL,
    make text NOT NULL,
    model text NOT NULL,
    year integer NOT NULL,
    vin text,
    license_plate text,
    odometer integer NOT NULL,
    last_service_odometer integer,
    service_interval_miles integer NOT NULL,
    next_service_due_on text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    deleted_at text
);


--
-- Name: follow_ups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.follow_ups (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    entity_type character varying(30) NOT NULL,
    entity_id character varying(64) NOT NULL,
    assigned_to character varying(200),
    due_date timestamp with time zone NOT NULL,
    note text,
    status character varying(20) NOT NULL,
    completed_at timestamp with time zone,
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    channel character varying(30),
    customer_id character varying(36),
    description text,
    follow_up_type character varying(50),
    job_id character varying(36),
    notes text,
    priority character varying(20),
    scheduled_at timestamp with time zone,
    title character varying(300)
);


--
-- Name: forecast_snapshot_invoices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.forecast_snapshot_invoices (
    id uuid NOT NULL,
    snapshot_id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    bucket character varying(10) NOT NULL,
    face_at_snapshot numeric(14,2) NOT NULL
);


--
-- Name: forecast_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.forecast_snapshots (
    id uuid NOT NULL,
    as_of date NOT NULL,
    window_days integer NOT NULL,
    horizon_end date NOT NULL,
    open_ar_face numeric(14,2) NOT NULL,
    assumed_rates json,
    status character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    reconciled_at timestamp with time zone,
    bucket_results json,
    CONSTRAINT ck_forecast_snapshots_window_pos CHECK ((window_days > 0))
);


--
-- Name: gdpr_data_access_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gdpr_data_access_logs (
    id character varying(36) NOT NULL,
    tenant_id text NOT NULL,
    user_id text,
    entity_type text NOT NULL,
    entity_id text,
    access_type text NOT NULL,
    fields_accessed text,
    request_id text,
    details text,
    created_at timestamp with time zone
);


--
-- Name: gl_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gl_accounts (
    id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(200) NOT NULL,
    type public.gl_account_type NOT NULL,
    parent_id uuid,
    role character varying(40),
    is_system boolean NOT NULL,
    active boolean NOT NULL,
    company_id character varying(36) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: gl_journal_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gl_journal_entries (
    id uuid NOT NULL,
    entry_no bigint NOT NULL,
    effective_at date NOT NULL,
    posted_at timestamp with time zone NOT NULL,
    status public.gl_entry_status NOT NULL,
    source_type character varying(50),
    source_id character varying(64),
    idempotency_key character varying(200),
    reverses_entry_id uuid,
    reversed_by_entry_id uuid,
    created_txid bigint,
    created_by character varying(36),
    company_id character varying(36) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: gl_journal_entry_no_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gl_journal_entry_no_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: gl_journal_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gl_journal_lines (
    id uuid NOT NULL,
    entry_id uuid NOT NULL,
    account_id uuid NOT NULL,
    amount_cents bigint NOT NULL,
    memo text,
    job_id uuid,
    customer_id uuid,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_gl_line_amount_nonzero CHECK ((amount_cents <> 0))
);


--
-- Name: gl_period_locks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gl_period_locks (
    id uuid NOT NULL,
    lock_date date NOT NULL,
    note text,
    created_by character varying(36),
    company_id character varying(36) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: gl_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gl_settings (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    ledger_posting_enabled boolean NOT NULL,
    reporting_basis character varying(10) NOT NULL,
    tax_basis character varying(10) NOT NULL,
    inventory_treatment character varying(20) NOT NULL,
    cutover_month date,
    entity_type character varying(40),
    opening_bank_attested_at timestamp with time zone,
    opening_bank_attested_by character varying(36),
    payment_method_role_map json,
    credit_reason_role_map json,
    expense_category_account_map json,
    revenue_category_account_map json,
    cpa_review json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: holding_areas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.holding_areas (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    name character varying(100) NOT NULL,
    color character varying(20),
    sort_order integer,
    created_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: hubx_door_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hubx_door_orders (
    id uuid NOT NULL,
    qcd character varying(40) NOT NULL,
    quantity integer,
    job_po_name character varying(200),
    model_number character varying(60),
    size character varying(60),
    submitted_text character varying(60),
    graph_message_id character varying(255) NOT NULL,
    received_at timestamp with time zone,
    outcome character varying(30) NOT NULL,
    matched_estimate_id uuid,
    matched_job_id uuid,
    parts_marked integer NOT NULL,
    moved_from_area character varying(100),
    moved_to_area character varying(100),
    created_at timestamp with time zone NOT NULL
);


--
-- Name: hubx_order_emails; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hubx_order_emails (
    id uuid NOT NULL,
    message_key character varying(998) NOT NULL,
    graph_message_id character varying(255) NOT NULL,
    received_at timestamp with time zone,
    item_count integer NOT NULL,
    new_item_count integer NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: inbound_emails; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inbound_emails (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    from_email character varying(254) NOT NULL,
    from_name character varying(200),
    to_email character varying(254) NOT NULL,
    subject character varying(500),
    body_text text,
    body_html text,
    provider character varying(30) NOT NULL,
    provider_message_id character varying(200),
    customer_id uuid,
    job_id uuid,
    has_attachments boolean NOT NULL,
    read_at timestamp with time zone,
    received_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: integration_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.integration_configs (
    id uuid NOT NULL,
    tenant_id character varying(50) NOT NULL,
    integration_type character varying(50) NOT NULL,
    name character varying(200) NOT NULL,
    webhook_url character varying(500) NOT NULL,
    secret text NOT NULL,
    events json NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    last_triggered_at timestamp with time zone,
    last_success_at timestamp with time zone
);


--
-- Name: internal_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.internal_tasks (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    assigned_to character varying(200),
    title character varying(300) NOT NULL,
    description text,
    priority character varying(20) NOT NULL,
    status character varying(20) NOT NULL,
    due_date timestamp with time zone,
    related_job_id uuid,
    related_customer_id uuid,
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: inventory_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inventory_items (
    id uuid NOT NULL,
    part_name character varying(200) NOT NULL,
    sku character varying(100),
    description text,
    quantity integer NOT NULL,
    reorder_level integer NOT NULL,
    unit_cost numeric(12,2) NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    supplier character varying(200),
    vendor_id character varying(100),
    category character varying(120),
    location character varying(120),
    manufacturer_part_number character varying(120),
    active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: invoice_adjustments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoice_adjustments (
    id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    kind public.invoice_adjustment_kind NOT NULL,
    amount numeric(12,2) NOT NULL,
    tax_component numeric(12,2) DEFAULT 0.00 NOT NULL,
    reason character varying(200),
    refund_method character varying(50),
    created_by character varying(64),
    created_at timestamp with time zone NOT NULL,
    company_id character varying(36) NOT NULL
);


--
-- Name: invoice_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoice_lines (
    id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    description text NOT NULL,
    quantity numeric(10,2) NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    line_total numeric(12,2) NOT NULL,
    taxable boolean DEFAULT true NOT NULL,
    category character varying(80),
    cost_snapshot numeric(12,2),
    margin_pct_snapshot numeric(6,4),
    margin_pct_override numeric(6,4),
    pricing_source character varying(32),
    pricing_inputs json,
    part_id character varying(36),
    labor_price_item_id uuid,
    estimated_man_hours numeric(6,2),
    labor_source character varying(16),
    includes_labor boolean DEFAULT false NOT NULL,
    source character varying(16),
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    company_id character varying(36) NOT NULL
);


--
-- Name: invoices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoices (
    id uuid NOT NULL,
    job_id uuid,
    estimate_id uuid,
    source_estimate_id uuid,
    invoice_number character varying(50) NOT NULL,
    billing_type public.invoice_billing_type NOT NULL,
    sequence_number integer NOT NULL,
    subtotal numeric(12,2) NOT NULL,
    tax_rate numeric(9,6),
    tax_amount numeric(12,2) NOT NULL,
    total numeric(12,2) NOT NULL,
    balance_due numeric(12,2) NOT NULL,
    totals_locked boolean DEFAULT false NOT NULL,
    dunning_paused boolean DEFAULT false NOT NULL,
    status public.invoice_status NOT NULL,
    due_date date,
    notes text,
    locked boolean NOT NULL,
    hide_line_prices boolean DEFAULT false NOT NULL,
    locked_at timestamp with time zone,
    sent_at timestamp with time zone,
    sent_via character varying(20),
    origin character varying(32),
    attached_photo_ids text,
    verified_at timestamp with time zone,
    verified_by_user_id character varying(36),
    adjusts_invoice_id uuid,
    paid_at timestamp with time zone,
    public_token character varying(64) NOT NULL,
    qb_dirty boolean DEFAULT true NOT NULL,
    qb_synced_at timestamp with time zone,
    customer_id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    invoice_date date,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    version integer DEFAULT 1 NOT NULL
);


--
-- Name: job_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_assignments (
    id character varying(36) NOT NULL,
    job_id character varying(36) NOT NULL,
    tech_id character varying(36) NOT NULL,
    user_id character varying(36),
    is_lead boolean NOT NULL,
    assigned_at timestamp with time zone NOT NULL,
    assigned_by character varying(36),
    en_route_at timestamp with time zone,
    arrived_at timestamp with time zone,
    completed_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: job_chat_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_chat_messages (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    job_id character varying(36) NOT NULL,
    sender_user_id character varying(200) NOT NULL,
    sender_role character varying(20) NOT NULL,
    sender_name character varying(200),
    kind character varying(20) NOT NULL,
    body text NOT NULL,
    quick_action character varying(40),
    read_by_user_id character varying(200),
    read_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: job_closeouts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_closeouts (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    parts_used json NOT NULL,
    no_parts_used boolean DEFAULT false NOT NULL,
    hours_worked numeric(8,2) NOT NULL,
    techs_on_site integer DEFAULT 1 NOT NULL,
    labor_matrix_item_id character varying(36),
    hourly_rate numeric(10,2),
    signature_data text,
    signed_by character varying(200),
    signed_at timestamp with time zone,
    notes text,
    closed_by_user_id character varying(36) NOT NULL,
    closed_at timestamp with time zone NOT NULL,
    superseded_at timestamp with time zone,
    supersedes_id uuid,
    is_restatement boolean,
    man_hours_billed numeric(8,2),
    billed_invoice_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: job_dependencies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_dependencies (
    id text NOT NULL,
    tenant_id text NOT NULL,
    job_id text NOT NULL,
    depends_on_job_id text NOT NULL,
    created_at text NOT NULL
);


--
-- Name: job_diagnoses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_diagnoses (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    service_type character varying(64) NOT NULL,
    data json NOT NULL,
    notes text,
    created_by character varying(200),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: job_hazards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_hazards (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    customer_id uuid,
    description text NOT NULL,
    severity character varying(20) NOT NULL,
    photo_url text,
    applies_to_customer boolean NOT NULL,
    created_by character varying(200),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: job_notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_notes (
    id character varying(36) NOT NULL,
    company_id character varying(64) NOT NULL,
    job_id character varying(36) NOT NULL,
    author_id character varying(200) NOT NULL,
    author_name character varying(200),
    body text NOT NULL,
    visibility character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: job_parts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_parts (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    part_id uuid NOT NULL,
    qty_used integer NOT NULL,
    unit_cost_at_time numeric(10,2) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: job_parts_needed; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_parts_needed (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    job_id character varying(36) NOT NULL,
    part_name character varying(200) NOT NULL,
    quantity integer,
    supplier character varying(200),
    urgency character varying(20),
    status character varying(20),
    notes text,
    sku character varying(255),
    requested_by_user_id character varying(36),
    photo_url text,
    eta_at timestamp with time zone,
    billed_invoice_id uuid,
    source character varying(20) DEFAULT 'request'::character varying NOT NULL,
    unit_price numeric(10,2),
    price_source character varying(24),
    created_at timestamp with time zone,
    updated_at timestamp with time zone
);


--
-- Name: job_photos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_photos (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    job_id uuid NOT NULL,
    kind character varying(20) NOT NULL,
    url character varying(1000) NOT NULL,
    filename character varying(255),
    mime_type character varying(100),
    size_bytes integer,
    caption character varying(500),
    uploaded_by character varying(200),
    customer_visible boolean DEFAULT false NOT NULL,
    uploaded_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone
);


--
-- Name: job_receipts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_receipts (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    vendor character varying(200),
    amount numeric(12,2),
    photo_url text,
    notes text,
    purchased_at timestamp with time zone,
    created_by character varying(200),
    promoted_expense_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: job_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_templates (
    id text NOT NULL,
    title text NOT NULL,
    job_type text NOT NULL,
    default_priority text NOT NULL,
    checklist text,
    estimated_duration integer NOT NULL,
    default_parts text,
    is_active integer NOT NULL,
    created_at text NOT NULL,
    updated_at text,
    deleted_at text
);


--
-- Name: jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jobs (
    id uuid NOT NULL,
    customer_id uuid,
    job_number character varying(50),
    title character varying(200) NOT NULL,
    description text,
    lifecycle_stage public.job_lifecycle_stage DEFAULT 'service_call'::public.job_lifecycle_stage NOT NULL,
    dispatch_status public.job_dispatch_status NOT NULL,
    billing_status public.job_billing_status DEFAULT 'unbilled'::public.job_billing_status NOT NULL,
    not_billable_at timestamp with time zone,
    not_billable_reason character varying(300),
    not_billable_by character varying(36),
    cancelled_at timestamp with time zone,
    cancel_reason character varying(300),
    scheduled_at timestamp with time zone,
    completed_at timestamp with time zone,
    assigned_to character varying(50),
    source character varying(50),
    is_return_visit boolean DEFAULT false NOT NULL,
    parent_job_id uuid,
    job_type character varying(100),
    status character varying(50),
    priority character varying(50),
    company_id character varying(36) NOT NULL,
    updated_at timestamp with time zone,
    is_demo boolean,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(36),
    deleted_at timestamp with time zone,
    approved_at timestamp with time zone,
    arrived_at timestamp with time zone,
    started_at timestamp with time zone,
    holding_area_id character varying(36),
    notes text,
    signature_data text,
    signed_at timestamp with time zone,
    signed_by character varying(200),
    scheduled_duration_hours numeric(5,2),
    location_id character varying(36)
);


--
-- Name: labor_price_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.labor_price_items (
    id uuid NOT NULL,
    sku character varying(40),
    description character varying(200) NOT NULL,
    service_type character varying(40) NOT NULL,
    width_ft integer,
    height_ft integer,
    flat_price numeric(12,2) NOT NULL,
    assumed_man_hours numeric(6,2) NOT NULL,
    default_crew_size integer DEFAULT 1 NOT NULL,
    min_wall_clock_minutes integer DEFAULT 15 NOT NULL,
    notes text,
    active boolean NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: landing_leads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.landing_leads (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    name character varying(200),
    email character varying(254),
    phone character varying(30),
    source character varying(100),
    message text,
    referrer character varying(500),
    utm_campaign character varying(200),
    utm_source character varying(200),
    utm_medium character varying(200),
    status character varying(20) NOT NULL,
    contacted_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: leads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leads (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    landing_lead_id uuid,
    name character varying(200) NOT NULL,
    email character varying(254),
    phone character varying(30),
    address character varying(500),
    stage character varying(20) NOT NULL,
    estimated_value numeric(12,2) NOT NULL,
    source character varying(100),
    assigned_to character varying(200),
    notes text,
    converted_customer_id uuid,
    converted_at timestamp with time zone,
    last_contact_at timestamp with time zone,
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    follow_up_date date,
    estimate_id uuid,
    origin_ref character varying(120),
    selected_estimate_id uuid,
    contacted_at timestamp with time zone,
    score numeric(5,2),
    status character varying(30)
);


--
-- Name: loyalty_points; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.loyalty_points (
    id uuid NOT NULL,
    customer_id character varying(64) NOT NULL,
    amount integer NOT NULL,
    reason character varying(200) NOT NULL,
    created_by character varying(64),
    created_at timestamp with time zone NOT NULL
);


--
-- Name: loyalty_referrals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.loyalty_referrals (
    id character varying(36) NOT NULL,
    tenant_id text,
    referrer_id text NOT NULL,
    referee_name text NOT NULL,
    referee_phone text NOT NULL,
    referee_email text,
    status text NOT NULL,
    converted_at text,
    rewarded_at text,
    reward_given boolean NOT NULL,
    created_at text,
    updated_at text,
    deleted_at text,
    company_id character varying(36) NOT NULL,
    converted_customer_id character varying(36)
);


--
-- Name: loyalty_tiers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.loyalty_tiers (
    id uuid NOT NULL,
    name character varying(100) NOT NULL,
    min_spend numeric(12,2) NOT NULL,
    discount_pct numeric(5,2) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: maintenance_plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.maintenance_plans (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    visits_per_year integer NOT NULL,
    billing_type character varying(20) NOT NULL,
    price numeric(12,2) NOT NULL,
    active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: margin_tiers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.margin_tiers (
    id uuid NOT NULL,
    tier_set_id uuid NOT NULL,
    cost_min numeric(12,2) NOT NULL,
    cost_max numeric(12,2),
    margin_pct numeric(6,4) NOT NULL,
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: marketing_campaigns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.marketing_campaigns (
    id uuid NOT NULL,
    company_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    type character varying(32) NOT NULL,
    status character varying(32) NOT NULL,
    subject text,
    body text,
    audience character varying(64),
    scheduled_at timestamp with time zone,
    last_sent_at timestamp with time zone,
    sent_count integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: markup_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.markup_rules (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    category character varying(100) NOT NULL,
    markup_percent numeric(6,2) NOT NULL,
    minimum_margin_percent numeric(6,2) NOT NULL,
    active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: message_thread_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.message_thread_members (
    thread_id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    joined_at timestamp with time zone,
    last_read_at timestamp with time zone
);


--
-- Name: message_threads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.message_threads (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    type character varying(20) NOT NULL,
    name character varying(200),
    created_by character varying(36) NOT NULL,
    created_at timestamp with time zone
);


--
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.messages (
    id character varying(36) NOT NULL,
    thread_id character varying(36) NOT NULL,
    sender_id character varying(36) NOT NULL,
    body text NOT NULL,
    job_id character varying(36),
    customer_id character varying(36),
    created_at timestamp with time zone
);


--
-- Name: monthly_budgets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.monthly_budgets (
    id character varying(36) NOT NULL,
    year integer NOT NULL,
    month integer NOT NULL,
    qb_account_id character varying(64) NOT NULL,
    account_name character varying(300),
    amount numeric(14,2) NOT NULL,
    line_type character varying(20) NOT NULL,
    pct_of_revenue numeric(6,4),
    source character varying(20) NOT NULL,
    is_locked boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: next_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.next_actions (
    id uuid NOT NULL,
    tenant_id character varying(100) NOT NULL,
    user_id character varying(100),
    action_type character varying(50) NOT NULL,
    title character varying(200) NOT NULL,
    description text,
    priority character varying(10) NOT NULL,
    action_url character varying(500),
    estimated_value double precision NOT NULL,
    reference_id character varying(100),
    status character varying(20) NOT NULL,
    snoozed_until timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: notification_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_log (
    id uuid NOT NULL,
    tenant_id character varying(50) NOT NULL,
    user_id character varying(50) NOT NULL,
    notification_type character varying(100) NOT NULL,
    channel character varying(20) NOT NULL,
    subject character varying(500),
    body text,
    status character varying(20) NOT NULL,
    sent_at timestamp with time zone,
    read_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: notification_preferences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_preferences (
    id uuid NOT NULL,
    tenant_id character varying(50) NOT NULL,
    user_id character varying(50) NOT NULL,
    notification_type character varying(100) NOT NULL,
    channel character varying(20) NOT NULL,
    is_enabled boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: notification_sent_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_sent_history (
    id text NOT NULL,
    tenant_id text NOT NULL,
    customer_id text NOT NULL,
    template_key text NOT NULL,
    channel text NOT NULL,
    status text NOT NULL,
    rendered_message text NOT NULL,
    sent_at text NOT NULL
);


--
-- Name: notification_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_templates (
    id text NOT NULL,
    tenant_id text NOT NULL,
    template_key text NOT NULL,
    subject text NOT NULL,
    body text NOT NULL,
    is_default integer NOT NULL,
    created_at text NOT NULL
);


--
-- Name: notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notifications (
    id text NOT NULL,
    tenant_id text NOT NULL,
    user_id text,
    title text NOT NULL,
    message text NOT NULL,
    category text NOT NULL,
    is_read integer NOT NULL,
    created_at text NOT NULL,
    deleted_at text
);


--
-- Name: notifications_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notifications_settings (
    tenant_id text NOT NULL,
    email_enabled integer NOT NULL,
    sms_enabled integer NOT NULL,
    sender_name text NOT NULL,
    updated_at text NOT NULL
);


--
-- Name: onboarding_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.onboarding_state (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    current_step character varying(50) NOT NULL,
    completed_steps text NOT NULL,
    completed_at timestamp with time zone,
    catalog_seeded boolean NOT NULL,
    demo_data_loaded boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: outbound_emails; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outbound_emails (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    initiator_kind character varying(20) NOT NULL,
    initiator_ref character varying(120),
    kind character varying(20),
    entity_type character varying(30),
    entity_id character varying(64),
    to_email text NOT NULL,
    to_name text,
    recipient_source character varying(20),
    recipient_contact_id character varying(36),
    subject text NOT NULL,
    body_html text NOT NULL,
    attachments_meta json,
    provider character varying(20),
    status character varying(12) NOT NULL,
    skip_reason character varying(60),
    bounced_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: outlook_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outlook_accounts (
    id uuid NOT NULL,
    user_id character varying(36) NOT NULL,
    provider character varying(40) NOT NULL,
    upn character varying(320),
    display_name character varying(200),
    access_token_enc text,
    refresh_token_enc text,
    access_token_expires_at timestamp with time zone,
    scopes text,
    connected_at timestamp with time zone,
    last_sync_at timestamp with time zone,
    last_error text,
    delta_token text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: outlook_attachments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outlook_attachments (
    id uuid NOT NULL,
    message_id uuid NOT NULL,
    graph_attachment_id character varying(255) NOT NULL,
    filename character varying(500) NOT NULL,
    content_type character varying(200),
    size_bytes integer,
    r2_key character varying(500),
    is_inline boolean NOT NULL,
    content_id character varying(255),
    created_at timestamp with time zone NOT NULL
);


--
-- Name: outlook_folder_prefs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outlook_folder_prefs (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    folder_id character varying(255) NOT NULL,
    color character varying(32),
    icon character varying(64),
    pinned boolean NOT NULL,
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: outlook_folder_sync_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outlook_folder_sync_state (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    folder_id character varying(255) NOT NULL,
    delta_token text,
    last_sync_at timestamp with time zone,
    last_error text,
    full_resync_required boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: outlook_folders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outlook_folders (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    graph_folder_id character varying(255) NOT NULL,
    display_name character varying(500) NOT NULL,
    parent_folder_id character varying(255),
    well_known_name character varying(64),
    total_count integer NOT NULL,
    unread_count integer NOT NULL,
    child_folder_count integer NOT NULL,
    is_hidden boolean NOT NULL,
    depth integer NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: outlook_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outlook_messages (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    graph_message_id character varying(255) NOT NULL,
    internet_message_id character varying(998),
    conversation_id character varying(255),
    subject text,
    from_address character varying(320),
    to_addresses json,
    cc_addresses json,
    bcc_addresses json,
    direction character varying(20) NOT NULL,
    sent_at timestamp with time zone,
    received_at timestamp with time zone,
    body_preview text,
    body_r2_key character varying(500),
    body_size_bytes integer,
    has_attachments boolean NOT NULL,
    linked_customer_id uuid,
    linked_job_id uuid,
    tag_strategy character varying(40),
    tag_confidence numeric(4,3),
    is_personal boolean NOT NULL,
    is_read boolean NOT NULL,
    is_flagged boolean NOT NULL,
    in_reply_to character varying(998),
    folder_id character varying(255),
    folder_display_name character varying(500),
    vendor_bills_ingested_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: outlook_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outlook_settings (
    id integer NOT NULL,
    backfill_days integer NOT NULL,
    tag_strategy_order json NOT NULL,
    tag_strategy_enabled json NOT NULL,
    ai_tag_threshold numeric(4,3) NOT NULL,
    visibility_rules json NOT NULL,
    auto_email_triggers json NOT NULL,
    vendor_bill_sender_allowlist json NOT NULL,
    payment_confirmation_sender_allowlist json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: outlook_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outlook_subscriptions (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    graph_subscription_id character varying(255) NOT NULL,
    notification_url text NOT NULL,
    client_state character varying(128) NOT NULL,
    expiration_at timestamp with time zone NOT NULL,
    last_renewed_at timestamp with time zone,
    last_error text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: overhead_obligations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.overhead_obligations (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    label character varying(200) NOT NULL,
    category character varying(40) NOT NULL,
    vendor_id uuid,
    amount numeric(14,2) NOT NULL,
    cadence public.overhead_cadence NOT NULL,
    start_date date NOT NULL,
    end_date date,
    term_total_occurrences integer,
    scheduled_changes json,
    cost_type character varying(20) NOT NULL,
    is_estimate boolean NOT NULL,
    source character varying(20) NOT NULL,
    source_stream_id uuid,
    active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: part_prices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.part_prices (
    id uuid NOT NULL,
    tenant_id character varying(100) NOT NULL,
    part_number character varying(100) NOT NULL,
    part_name character varying(200) NOT NULL,
    cost_price numeric(12,2) NOT NULL,
    sell_price numeric(12,2) NOT NULL,
    margin_pct numeric(6,4) NOT NULL,
    supplier character varying(200),
    last_updated_at timestamp with time zone NOT NULL,
    price_history json,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: parts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.parts (
    id uuid NOT NULL,
    sku character varying(50) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    unit_cost numeric(10,2) NOT NULL,
    unit_price numeric(10,2) NOT NULL,
    qty_on_hand integer NOT NULL,
    reorder_point integer NOT NULL,
    vendor_name character varying(200),
    vendor_sku character varying(100),
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: payment_plan_installments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_plan_installments (
    id uuid NOT NULL,
    plan_id uuid NOT NULL,
    seq integer NOT NULL,
    due_date date NOT NULL,
    amount numeric(12,2) NOT NULL,
    status character varying(20) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: payment_plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_plans (
    id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    num_installments integer NOT NULL,
    total_amount numeric(12,2) NOT NULL,
    start_date date NOT NULL,
    created_by character varying(64),
    created_at timestamp with time zone NOT NULL,
    cancelled_at timestamp with time zone,
    cancelled_by character varying(64)
);


--
-- Name: payment_reminders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_reminders (
    id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    customer_id uuid,
    customer_name character varying(200),
    stage character varying(40) NOT NULL,
    channel character varying(20) NOT NULL,
    sent_at timestamp with time zone,
    sent_by character varying(200),
    notes text,
    threshold_days integer,
    promised_payment_date timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payments (
    id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    amount numeric(12,2) NOT NULL,
    method character varying(50) NOT NULL,
    surcharge_amount numeric(12,2),
    payment_date date NOT NULL,
    reference character varying(200),
    voided_at timestamp with time zone,
    voided_reason character varying(64),
    created_at timestamp with time zone NOT NULL,
    company_id character varying(36) NOT NULL
);


--
-- Name: payroll_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_entries (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    tech_user_id character varying(36) NOT NULL,
    period_start timestamp with time zone NOT NULL,
    period_end timestamp with time zone NOT NULL,
    hours_paid numeric(10,2) NOT NULL,
    gross_pay numeric(12,2) NOT NULL,
    source character varying(40) NOT NULL,
    external_ref character varying(100),
    notes text,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: pdf_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pdf_templates (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    template_type character varying(50) NOT NULL,
    brand_color character varying(20),
    font_family character varying(50),
    header_content text,
    footer_content text,
    blocks text NOT NULL,
    logo_url text,
    created_at timestamp with time zone,
    updated_at timestamp with time zone
);


--
-- Name: performance_slow_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.performance_slow_events (
    id character varying(36) NOT NULL,
    event_type text NOT NULL,
    tenant_id text NOT NULL,
    request_id text,
    path text,
    sql_text text,
    params_json text,
    duration_ms integer NOT NULL,
    details text,
    created_at timestamp with time zone
);


--
-- Name: phone_com_calls; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_com_calls (
    id uuid NOT NULL,
    phone_com_call_id character varying(80) NOT NULL,
    direction character varying(10) NOT NULL,
    from_number character varying(40),
    to_number character varying(40),
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    duration_s integer,
    status character varying(40),
    final_action_target character varying(80),
    extension_id character varying(80),
    recording_url text,
    recording_fetched_at timestamp with time zone,
    customer_id uuid,
    job_id uuid,
    raw_payload json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: phone_com_contact_push; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_com_contact_push (
    id uuid NOT NULL,
    customer_id uuid NOT NULL,
    phone_e164 character varying(40) NOT NULL,
    phone_com_contact_id character varying(80),
    last_pushed_at timestamp with time zone NOT NULL,
    last_error text,
    name_pushed character varying(200)
);


--
-- Name: phone_com_extensions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_com_extensions (
    id uuid NOT NULL,
    phone_com_extension_id character varying(80) NOT NULL,
    name character varying(200),
    number character varying(40),
    user_id uuid,
    is_active boolean NOT NULL,
    last_synced_at timestamp with time zone,
    preferred_outbound_did character varying(40)
);


--
-- Name: phone_com_faxes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_com_faxes (
    id uuid NOT NULL,
    phone_com_fax_id character varying(80) NOT NULL,
    direction character varying(10) NOT NULL,
    from_number character varying(40),
    to_number character varying(40),
    pages integer,
    status character varying(40),
    received_at timestamp with time zone,
    pdf_url text,
    pdf_fetched_at timestamp with time zone,
    customer_id uuid,
    job_id uuid,
    heard_at timestamp with time zone,
    heard_by_user_id uuid,
    raw_payload json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: phone_com_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_com_messages (
    id uuid NOT NULL,
    phone_com_message_id character varying(80) NOT NULL,
    phone_com_conversation_id character varying(80),
    thread_key character varying(100) NOT NULL,
    direction character varying(10) NOT NULL,
    from_number character varying(40),
    to_number character varying(40),
    body text,
    sent_at timestamp with time zone,
    received_at timestamp with time zone,
    read_at timestamp with time zone,
    delivery_status character varying(40),
    delivery_failed_reason text,
    attachments json NOT NULL,
    media_fetched_at timestamp with time zone,
    customer_id uuid,
    job_id uuid,
    sent_by_user_id uuid,
    raw_payload json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: phone_com_numbers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_com_numbers (
    id uuid NOT NULL,
    phone_com_number character varying(40) NOT NULL,
    label character varying(200),
    is_default_outbound boolean NOT NULL,
    campaign_tag character varying(100),
    last_synced_at timestamp with time zone
);


--
-- Name: phone_com_stats_daily; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_com_stats_daily (
    id uuid NOT NULL,
    stat_date date NOT NULL,
    calls_in integer NOT NULL,
    calls_out integer NOT NULL,
    calls_missed integer NOT NULL,
    sms_in integer NOT NULL,
    sms_out integer NOT NULL,
    voicemails_new integer NOT NULL,
    total_call_minutes integer NOT NULL,
    raw_payload json NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: phone_com_voicemails; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_com_voicemails (
    id uuid NOT NULL,
    phone_com_voicemail_id character varying(80) NOT NULL,
    call_id uuid,
    extension_id character varying(80),
    duration_s integer,
    audio_url text,
    audio_fetched_at timestamp with time zone,
    transcript text,
    transcript_source character varying(20),
    heard_at timestamp with time zone,
    heard_by_user_id uuid,
    raw_payload json NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: plan_enrollments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plan_enrollments (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    plan_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    status character varying(20) NOT NULL,
    start_date timestamp with time zone NOT NULL,
    next_service_date timestamp with time zone,
    visits_completed integer NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: plan_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plan_steps (
    id character varying(36) NOT NULL,
    plan_id character varying(36) NOT NULL,
    title character varying(300) NOT NULL,
    assigned_to character varying(36),
    status character varying(20),
    due_date timestamp with time zone,
    sort_order integer
);


--
-- Name: planner_day_notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.planner_day_notes (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    note_date date NOT NULL,
    body text DEFAULT ''::text NOT NULL,
    updated_by character varying(36),
    updated_via character varying(20),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: planner_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.planner_tasks (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    title character varying(300) NOT NULL,
    description text,
    status character varying(20) NOT NULL,
    priority character varying(20) NOT NULL,
    due_date timestamp with time zone,
    created_by character varying(36) NOT NULL,
    assigned_to character varying(36),
    job_id character varying(36),
    customer_id character varying(36),
    contact_phone character varying(40),
    phone_com_call_id character varying(80),
    source character varying(20),
    created_at timestamp with time zone,
    completed_at timestamp with time zone,
    today_date timestamp with time zone,
    remind_at timestamp with time zone,
    remind_user_id character varying(36),
    reminder_sent_at timestamp with time zone
);


--
-- Name: plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plans (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    title character varying(300) NOT NULL,
    description text,
    is_template boolean,
    created_by character varying(36) NOT NULL,
    shared_with text,
    created_at timestamp with time zone
);


--
-- Name: plugin_email_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plugin_email_outbox (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    plugin_key character varying(80) NOT NULL,
    delivery_id character varying(80) NOT NULL,
    to_email text,
    customer_id character varying(36),
    contact_id character varying(36),
    subject text NOT NULL,
    body_text text,
    body_html text,
    entity_type character varying(30),
    entity_id character varying(64),
    status character varying(12) NOT NULL,
    attempts integer NOT NULL,
    last_error character varying(120),
    created_at timestamp with time zone NOT NULL,
    processed_at timestamp with time zone
);


--
-- Name: portal_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_messages (
    id text NOT NULL,
    customer_id text NOT NULL,
    subject text NOT NULL,
    message text NOT NULL,
    status text NOT NULL,
    created_at text NOT NULL
);


--
-- Name: pricing_class_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pricing_class_settings (
    pricing_class public.pricing_class NOT NULL,
    rolling_volume_discount_enabled boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: pricing_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pricing_settings (
    id uuid NOT NULL,
    volume_discount_enabled boolean NOT NULL,
    loaded_labor_cost_per_hour numeric(8,2) DEFAULT '0'::numeric NOT NULL,
    target_labor_blended_rate_per_hour numeric(8,2) DEFAULT '100'::numeric NOT NULL,
    service_call_first_hour_price numeric(8,2) DEFAULT '100'::numeric NOT NULL,
    service_call_hourly_rate numeric(8,2) DEFAULT '100'::numeric NOT NULL,
    service_labor_description_template text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: pricing_tier_sets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pricing_tier_sets (
    id uuid NOT NULL,
    pricing_category character varying(40) NOT NULL,
    pricing_class public.pricing_class NOT NULL,
    active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: pricing_tiers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pricing_tiers (
    id uuid NOT NULL,
    wholesaler_tenant_id character varying(100) NOT NULL,
    distributor_tenant_id character varying(100) NOT NULL,
    tier_name character varying(50) NOT NULL,
    discount_pct numeric(5,2) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: proposal_tier_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proposal_tier_lines (
    id uuid NOT NULL,
    tier_id uuid NOT NULL,
    description text NOT NULL,
    category character varying(80),
    quantity integer NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    line_total numeric(12,2) NOT NULL,
    sort_order integer NOT NULL,
    company_id character varying(36) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: proposal_tiers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proposal_tiers (
    id uuid NOT NULL,
    estimate_id uuid NOT NULL,
    tier_name public.proposal_tier_name NOT NULL,
    description text,
    total_price numeric(12,2) NOT NULL,
    includes_parts boolean NOT NULL,
    warranty_months integer NOT NULL,
    stripe_payment_link character varying(500),
    display_order integer NOT NULL
);


--
-- Name: purchase_order_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.purchase_order_lines (
    id uuid NOT NULL,
    po_id uuid NOT NULL,
    item_id uuid,
    sku character varying(100),
    description character varying(500) NOT NULL,
    quantity_ordered integer NOT NULL,
    quantity_received integer NOT NULL,
    unit_cost numeric(12,2) NOT NULL,
    line_total numeric(12,2) NOT NULL
);


--
-- Name: purchase_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.purchase_orders (
    id uuid NOT NULL,
    po_number character varying(50) NOT NULL,
    vendor_id uuid,
    vendor_name character varying(200),
    status character varying(30) NOT NULL,
    order_date date NOT NULL,
    expected_date date,
    received_date date,
    job_id uuid,
    door_snapshot json,
    notes text,
    subtotal numeric(12,2) NOT NULL,
    tax numeric(12,2) NOT NULL,
    shipping numeric(12,2) NOT NULL,
    total numeric(12,2) NOT NULL,
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: push_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.push_subscriptions (
    id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    endpoint text NOT NULL,
    p256dh text NOT NULL,
    auth text NOT NULL,
    user_agent character varying(500),
    subscribed_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone,
    revoked_at timestamp with time zone
);


--
-- Name: qb_bank_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_bank_transactions (
    id uuid NOT NULL,
    qb_txn_id character varying(120) NOT NULL,
    txn_date date,
    txn_type character varying(50),
    account_name character varying(300),
    payee character varying(300),
    amount numeric(14,2) NOT NULL,
    memo text,
    category character varying(300),
    status character varying(50),
    synced_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: qb_banking_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_banking_entries (
    id uuid NOT NULL,
    qb_entity character varying(40) NOT NULL,
    qb_txn_id character varying(64) NOT NULL,
    qb_line_index numeric(4,0) DEFAULT '0'::numeric NOT NULL,
    txn_date date,
    amount numeric(14,2) NOT NULL,
    account_id character varying(64),
    account_name character varying(300),
    counterparty_name character varying(300),
    memo text,
    raw_json json,
    last_synced_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: qb_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_connections (
    id uuid NOT NULL,
    tenant_id character varying(64) NOT NULL,
    realm_id character varying(100) NOT NULL,
    access_token text NOT NULL,
    refresh_token text NOT NULL,
    access_token_expires_at timestamp with time zone NOT NULL,
    refresh_token_expires_at timestamp with time zone NOT NULL,
    last_sync_at timestamp with time zone,
    error_count integer NOT NULL,
    last_error text,
    delete_sync_enabled boolean,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: qb_deposits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_deposits (
    id uuid NOT NULL,
    qb_txn_id character varying(64) NOT NULL,
    txn_date date,
    total_amount numeric(14,2) NOT NULL,
    deposit_to_account_id character varying(64),
    deposit_to_account_name character varying(300),
    memo text,
    linked_qb_ids text,
    raw_json json,
    last_synced_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: qb_entity_maps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_entity_maps (
    id uuid NOT NULL,
    tenant_id character varying(64) NOT NULL,
    entity_type character varying(40) NOT NULL,
    local_id character varying(64) NOT NULL,
    qb_id character varying(120) NOT NULL,
    synced_at timestamp with time zone NOT NULL
);


--
-- Name: qb_pnl_monthly; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_pnl_monthly (
    id character varying(36) NOT NULL,
    year integer NOT NULL,
    month integer NOT NULL,
    qb_account_id character varying(64) NOT NULL,
    account_name character varying(300),
    account_type character varying(100),
    amount numeric(14,2) NOT NULL,
    synced_at timestamp with time zone NOT NULL
);


--
-- Name: qb_recurring_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_recurring_transactions (
    id uuid NOT NULL,
    qb_id character varying(64) NOT NULL,
    txn_type character varying(40) NOT NULL,
    name character varying(200),
    customer_qb_id character varying(64),
    customer_name character varying(200),
    amount numeric(14,2) NOT NULL,
    next_date date,
    interval_type character varying(20),
    num_interval numeric(4,0),
    days_of_week character varying(60),
    active boolean NOT NULL,
    raw_json json,
    last_synced_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: qb_sync_schedule; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_sync_schedule (
    id uuid NOT NULL,
    frequency character varying(20) DEFAULT 'manual'::character varying NOT NULL,
    last_run_at timestamp with time zone,
    next_run_at timestamp with time zone,
    last_run_status character varying(40),
    last_run_error text,
    last_success_at timestamp with time zone,
    last_run_reads integer,
    last_full_sync_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: qb_token_store; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_token_store (
    id uuid NOT NULL,
    tenant_id character varying(64) NOT NULL,
    realm_id character varying(50) NOT NULL,
    environment character varying(20) NOT NULL,
    access_token_enc text NOT NULL,
    refresh_token_enc text NOT NULL,
    access_token_expires_at timestamp with time zone NOT NULL,
    refresh_token_expires_at timestamp with time zone NOT NULL,
    auth_state character varying(32) DEFAULT 'healthy'::character varying NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: qb_transfers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_transfers (
    id uuid NOT NULL,
    qb_txn_id character varying(64) NOT NULL,
    txn_date date,
    amount numeric(14,2) NOT NULL,
    from_account_id character varying(64),
    from_account_name character varying(300),
    to_account_id character varying(64),
    to_account_name character varying(300),
    memo text,
    raw_json json,
    last_synced_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: qb_vendors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_vendors (
    id uuid NOT NULL,
    tenant_id character varying(64) NOT NULL,
    qb_vendor_id character varying(120) NOT NULL,
    name character varying(200) NOT NULL,
    email character varying(255),
    phone character varying(50),
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: qb_webhook_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_webhook_events (
    id uuid NOT NULL,
    event_id character varying(200) NOT NULL,
    event_type character varying(100) NOT NULL,
    entity_id character varying(100) NOT NULL,
    realm_id character varying(50) NOT NULL,
    processed_at timestamp with time zone NOT NULL
);


--
-- Name: quote_request_photos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.quote_request_photos (
    id uuid NOT NULL,
    quote_request_id uuid NOT NULL,
    door_index integer NOT NULL,
    filename character varying(255) NOT NULL,
    content_type character varying(80) DEFAULT 'image/jpeg'::character varying NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: quote_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.quote_requests (
    id uuid NOT NULL,
    customer_id uuid NOT NULL,
    submitted_by_user_id uuid NOT NULL,
    lead_id uuid,
    job_name character varying(200) NOT NULL,
    site_address character varying(500),
    notes text,
    doors json NOT NULL,
    doors_version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    edited_at timestamp with time zone,
    withdrawn_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: quote_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.quote_templates (
    id uuid NOT NULL,
    tenant_id character varying(100) NOT NULL,
    job_type character varying(100) NOT NULL,
    typical_parts json,
    typical_labor_hours numeric(6,2),
    typical_price_low numeric(12,2),
    typical_price_high numeric(12,2),
    last_used_at timestamp with time zone,
    use_count integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: recurring_job_schedules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recurring_job_schedules (
    id text NOT NULL,
    job_template_id text NOT NULL,
    frequency text NOT NULL,
    customer_id text NOT NULL,
    next_run text NOT NULL,
    last_run text,
    status text NOT NULL,
    created_at text NOT NULL,
    updated_at text,
    deleted_at text
);


--
-- Name: recurring_stream_hits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recurring_stream_hits (
    id uuid NOT NULL,
    stream_id uuid NOT NULL,
    qb_txn_id character varying(120) NOT NULL,
    txn_date date NOT NULL,
    amount numeric(14,2) NOT NULL,
    confirmed boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: recurring_streams; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recurring_streams (
    id uuid NOT NULL,
    label character varying(200) NOT NULL,
    source character varying(20) NOT NULL,
    payee_pattern character varying(200) NOT NULL,
    amount_min numeric(14,2) NOT NULL,
    amount_max numeric(14,2) NOT NULL,
    account_name character varying(300),
    cadence character varying(20) NOT NULL,
    cadence_anchor_day integer,
    term_total_occurrences integer,
    term_end_date date,
    status character varying(20) NOT NULL,
    occurrences_seen integer DEFAULT 0 NOT NULL,
    start_date date,
    next_expected_date date,
    last_observed_date date,
    ended_at date,
    ended_reason character varying(40),
    notes text,
    created_by_user_id uuid,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT ck_recurring_streams_amount_window CHECK ((amount_min <= amount_max)),
    CONSTRAINT ck_recurring_streams_anchor_day CHECK (((cadence_anchor_day IS NULL) OR ((cadence_anchor_day >= 1) AND (cadence_anchor_day <= 31)))),
    CONSTRAINT ck_recurring_streams_occurrences_seen CHECK ((occurrences_seen >= 0)),
    CONSTRAINT ck_recurring_streams_term_xor CHECK (((term_total_occurrences IS NULL) OR (term_end_date IS NULL)))
);


--
-- Name: reminder_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reminder_settings (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    enabled boolean NOT NULL,
    auto_send_enabled boolean DEFAULT false NOT NULL,
    auto_send_nudge_dismissed boolean DEFAULT false NOT NULL,
    schedule_days text NOT NULL,
    subject_template character varying(500) NOT NULL,
    body_template text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: resale_quotes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resale_quotes (
    id uuid NOT NULL,
    customer_id uuid NOT NULL,
    estimate_id uuid NOT NULL,
    created_by uuid NOT NULL,
    reference character varying(60) NOT NULL,
    end_customer_name character varying(200),
    end_customer_address character varying(500),
    notes text,
    markup_pct numeric(6,2) NOT NULL,
    base_subtotal numeric(12,2),
    resale_subtotal numeric(12,2),
    lines_snapshot json NOT NULL,
    hide_line_prices boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: reseller_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reseller_profiles (
    id uuid NOT NULL,
    customer_id uuid NOT NULL,
    company_name character varying(200),
    phone character varying(50),
    email character varying(200),
    address character varying(500),
    website character varying(200),
    license_no character varying(100),
    logo_file character varying(255),
    terms_text text,
    default_markup_pct numeric(6,2) DEFAULT '0'::numeric NOT NULL,
    disclaimer_version character varying(32),
    disclaimer_accepted_at timestamp with time zone,
    disclaimer_accepted_by uuid,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: resources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resources (
    id uuid NOT NULL,
    company_id text NOT NULL,
    name text NOT NULL,
    description text,
    category text NOT NULL,
    file_path text NOT NULL,
    file_size integer,
    mime_type text,
    version text,
    download_count integer,
    created_by text,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: role_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.role_permissions (
    role text NOT NULL,
    permissions text NOT NULL,
    updated_at timestamp without time zone
);


--
-- Name: saved_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.saved_reports (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    report_type character varying(50) NOT NULL,
    config json,
    created_by character varying(50),
    created_at timestamp with time zone NOT NULL
);


--
-- Name: scheduled_sms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scheduled_sms (
    id uuid NOT NULL,
    kind character varying(20) NOT NULL,
    entity_id uuid,
    to_number character varying(40) NOT NULL,
    body text,
    customer_id uuid,
    job_id uuid,
    send_at timestamp with time zone NOT NULL,
    status character varying(20) NOT NULL,
    audit_action character varying(60) NOT NULL,
    tenant_id character varying(36) NOT NULL,
    created_by_user_id uuid NOT NULL,
    canceled_by_user_id uuid,
    canceled_at timestamp with time zone,
    attempted_at timestamp with time zone,
    sent_at timestamp with time zone,
    phone_com_message_row_id uuid,
    error_code character varying(60),
    error_message text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: security_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_events (
    id character varying(36) NOT NULL,
    tenant_id text NOT NULL,
    user_id text,
    event_type text NOT NULL,
    details text,
    ip_address text,
    request_id text,
    created_at timestamp with time zone
);


--
-- Name: segments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.segments (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    rules json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: service_agreement_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_agreement_templates (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    default_duration_months integer NOT NULL,
    default_price numeric(12,2) NOT NULL,
    services_included text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: service_agreements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_agreements (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    customer_id uuid NOT NULL,
    template_id uuid,
    name character varying(200) NOT NULL,
    status character varying(20) NOT NULL,
    start_date timestamp with time zone NOT NULL,
    end_date timestamp with time zone NOT NULL,
    price numeric(12,2) NOT NULL,
    services_included text,
    notes text,
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: service_locations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_locations (
    id uuid NOT NULL,
    tenant_id character varying(100) NOT NULL,
    name character varying(200) NOT NULL,
    address character varying(300),
    city character varying(100),
    state character varying(50),
    zip character varying(20),
    phone character varying(30),
    email character varying(200),
    timezone character varying(60) NOT NULL,
    is_primary boolean NOT NULL,
    is_active boolean NOT NULL,
    manager_user_id character varying(36),
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: service_triggers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_triggers (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    agreement_id character varying(36) NOT NULL,
    customer_id character varying(36) NOT NULL,
    next_due timestamp with time zone NOT NULL,
    interval_months integer NOT NULL,
    auto_create_job boolean,
    last_triggered timestamp with time zone,
    status character varying(20) NOT NULL,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: stock_adjustments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stock_adjustments (
    id uuid NOT NULL,
    item_id uuid NOT NULL,
    quantity_delta integer NOT NULL,
    reason character varying(60) NOT NULL,
    notes text,
    job_id uuid,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: stripe_webhook_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stripe_webhook_events (
    event_id character varying(255) NOT NULL,
    event_type character varying(100) NOT NULL,
    result_status character varying(64),
    processed_at timestamp with time zone NOT NULL
);


--
-- Name: support_tickets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.support_tickets (
    id character varying(36) NOT NULL,
    tenant_id character varying(36) NOT NULL,
    opened_by_email character varying(200) NOT NULL,
    opened_by_user_id character varying(36),
    subject character varying(200) NOT NULL,
    body text NOT NULL,
    category character varying(20) NOT NULL,
    priority character varying(20) NOT NULL,
    status character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    closed_at timestamp with time zone,
    resolution_summary text
);


--
-- Name: survey_responses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.survey_responses (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    send_id uuid NOT NULL,
    template_id uuid NOT NULL,
    score integer NOT NULL,
    comment text,
    submitted_ip character varying(45),
    created_at timestamp with time zone NOT NULL
);


--
-- Name: survey_sends; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.survey_sends (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    template_id uuid NOT NULL,
    customer_id uuid,
    job_id uuid,
    recipient_email character varying(254),
    recipient_phone character varying(30),
    token character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    responded_at timestamp with time zone,
    sent_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: survey_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.survey_templates (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    name character varying(200) NOT NULL,
    kind character varying(20) NOT NULL,
    question character varying(500) NOT NULL,
    follow_up_question character varying(500),
    active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: tag_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tag_assignments (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    tag_id uuid NOT NULL,
    entity_type character varying(30) NOT NULL,
    entity_id character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tags (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    name character varying(80) NOT NULL,
    color character varying(20) NOT NULL,
    description character varying(500),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: tax_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_config (
    id uuid NOT NULL,
    name character varying(100) NOT NULL,
    default_rate numeric(9,6) NOT NULL,
    tax_labor boolean DEFAULT false NOT NULL,
    description text,
    configured_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tax_exemption; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_exemption (
    id uuid NOT NULL,
    customer_id uuid NOT NULL,
    exempt boolean NOT NULL,
    reason character varying(200),
    certificate_id character varying(100),
    exempt_from date,
    exempt_until date,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tax_jurisdictions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_jurisdictions (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    name character varying(200) NOT NULL,
    rate numeric(9,6) NOT NULL,
    is_default boolean,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: team_message_recipients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.team_message_recipients (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    message_id uuid NOT NULL,
    recipient_id character varying(200) NOT NULL,
    read_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: team_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.team_messages (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    sender_id character varying(200) NOT NULL,
    sender_name character varying(200),
    subject character varying(300),
    body text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: tech_commission_rates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tech_commission_rates (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    tech_id character varying(64) NOT NULL,
    rate_type character varying(20) NOT NULL,
    rate_value numeric(10,2) NOT NULL,
    effective_from timestamp with time zone NOT NULL,
    effective_until timestamp with time zone,
    active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: tech_locations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tech_locations (
    id uuid NOT NULL,
    user_id character varying(200) NOT NULL,
    technician_id character varying(64),
    job_id uuid,
    lat numeric(10,7) NOT NULL,
    lng numeric(10,7) NOT NULL,
    accuracy_m numeric(8,2),
    speed_mps numeric(8,2),
    heading_deg numeric(6,2),
    recorded_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: technician_locations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.technician_locations (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    tech_id character varying(64) NOT NULL,
    lat numeric(10,7) NOT NULL,
    lng numeric(10,7) NOT NULL,
    accuracy_meters numeric(10,2),
    speed_mph numeric(6,2),
    heading_deg integer,
    battery_percent integer,
    recorded_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    "timestamp" timestamp with time zone,
    accuracy double precision,
    battery_pct integer,
    heading double precision,
    status character varying(50),
    technician_id character varying(36)
);


--
-- Name: technicians; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.technicians (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    user_id character varying(36),
    name character varying(200),
    email character varying(254),
    phone character varying(50),
    skills text,
    hourly_rate numeric(10,2),
    active boolean,
    territory character varying(200),
    availability_status character varying(30),
    commission_pct numeric(5,2),
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: tenant_forecast_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_forecast_settings (
    id uuid NOT NULL,
    default_window_days numeric(4,0) DEFAULT '30'::numeric NOT NULL,
    collect_rate_0_30 numeric(5,4) NOT NULL,
    collect_rate_31_60 numeric(5,4) NOT NULL,
    collect_rate_61_90 numeric(5,4) NOT NULL,
    collect_rate_90_plus numeric(5,4) NOT NULL,
    scheduled_realization_rate numeric(5,4) NOT NULL,
    include_recurring boolean DEFAULT true NOT NULL,
    cash_floor numeric(14,2),
    operating_account_ids json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tenant_roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_roles (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    name character varying(100) NOT NULL,
    description character varying(500),
    permissions text NOT NULL,
    is_system boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: time_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.time_entries (
    id uuid NOT NULL,
    job_id uuid,
    tech_id character varying(80) NOT NULL,
    clock_in timestamp with time zone NOT NULL,
    clock_out timestamp with time zone,
    duration_minutes integer,
    entry_type character varying(50) DEFAULT 'manual'::character varying NOT NULL,
    hourly_rate numeric(10,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    day_closed_at timestamp with time zone,
    billed_invoice_id uuid,
    company_id character varying(36) NOT NULL,
    entry_type_old character varying(50),
    gps_lat numeric(10,7),
    gps_lng numeric(10,7),
    notes text,
    signature_data text,
    signed_by character varying(200),
    tech_name character varying(200),
    technician_id character varying(36),
    user_id character varying(36)
);


--
-- Name: time_off_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.time_off_requests (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    technician_id character varying(36) NOT NULL,
    entry_type character varying(20) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    minutes_per_day integer NOT NULL,
    notes text,
    status character varying(20) NOT NULL,
    requested_by character varying(36) NOT NULL,
    reviewed_by character varying(36),
    reviewed_at timestamp with time zone,
    review_note text,
    entry_ids json DEFAULT '[]'::json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: timeclock_breaks_router; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.timeclock_breaks_router (
    id text NOT NULL,
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    time_entry_id text,
    type text NOT NULL,
    notes text,
    started_at text NOT NULL,
    ended_at text,
    duration_minutes integer,
    created_at text NOT NULL
);


--
-- Name: timeclock_entries_router; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.timeclock_entries_router (
    id text NOT NULL,
    tenant_id text NOT NULL,
    technician_id text NOT NULL,
    clock_in_at text NOT NULL,
    clock_out_at text,
    minutes integer,
    notes text,
    entry_type text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    deleted_at text
);


--
-- Name: timeclocks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.timeclocks (
    id uuid NOT NULL,
    technician_id character varying(50) NOT NULL,
    job_id uuid,
    clock_in_at timestamp with time zone NOT NULL,
    clock_out_at timestamp with time zone,
    labor_minutes integer,
    notes text,
    created_at timestamp with time zone NOT NULL,
    clock_in timestamp with time zone,
    clock_out timestamp with time zone,
    company_id character varying(36) NOT NULL,
    duration_minutes integer,
    gps_accuracy double precision,
    lat double precision,
    lng double precision,
    signature_data text,
    signed_by character varying(255),
    tenant_id character varying(50),
    user_id character varying(36)
);


--
-- Name: user_locations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_locations (
    id uuid NOT NULL,
    user_id character varying(36) NOT NULL,
    location_id uuid NOT NULL,
    can_dispatch_for boolean NOT NULL
);


--
-- Name: user_role_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_role_assignments (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    user_id character varying(200) NOT NULL,
    role_id uuid NOT NULL,
    assigned_by character varying(200),
    assigned_at timestamp with time zone NOT NULL
);


--
-- Name: user_tour_progress; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_tour_progress (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    tour_id character varying(64) NOT NULL,
    version integer NOT NULL,
    status character varying(16) NOT NULL,
    last_step integer,
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid NOT NULL,
    username character varying(200),
    email character varying(254),
    name character varying(200),
    full_name character varying(200),
    password_hash character varying(256),
    role character varying(50),
    active boolean,
    schedulable boolean,
    company_id character varying(36) NOT NULL,
    phone character varying(50),
    route_start_address character varying(500),
    must_change_password boolean,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone,
    address text,
    auth_email character varying(254),
    google_email character varying(254),
    google_id character varying(200),
    department character varying(100),
    hire_date date,
    hourly_rate numeric(10,2),
    commission_pct numeric(5,2),
    certifications text,
    hr_notes text,
    "position" character varying(200),
    field_skills text,
    field_territory character varying(200),
    route_start_lat numeric(10,7),
    route_start_lng numeric(10,7),
    availability_status character varying(30),
    failed_login_count integer,
    locked_until timestamp with time zone,
    mfa_enabled boolean,
    mfa_secret character varying(200),
    last_seen timestamp with time zone,
    tc_can_approve boolean,
    tc_can_edit boolean,
    tc_can_view_others boolean,
    tc_permissions text,
    emergency_contact_name character varying(200),
    emergency_contact_phone character varying(50),
    mcp_enabled boolean,
    shift_start time without time zone,
    shift_end time without time zone,
    workdays smallint,
    vacation_allowance_hours numeric(6,2)
);


--
-- Name: vehicle_inspections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vehicle_inspections (
    id uuid NOT NULL,
    vehicle_id character varying(64),
    vehicle_label character varying(200),
    technician_id character varying(200) NOT NULL,
    inspection_type character varying(30) NOT NULL,
    odometer integer,
    fuel_cost numeric(10,2),
    fuel_gallons numeric(8,3),
    photo_url text,
    issues_found text,
    notes text,
    inspection_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: vehicle_service_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vehicle_service_records (
    id uuid NOT NULL,
    vehicle_id uuid NOT NULL,
    service_type public.vehicle_service_type NOT NULL,
    mileage_at_service integer NOT NULL,
    service_date timestamp with time zone NOT NULL,
    cost numeric(10,2),
    notes text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: vehicles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vehicles (
    id uuid NOT NULL,
    vin character varying(17),
    make character varying(100) NOT NULL,
    model character varying(100) NOT NULL,
    year integer NOT NULL,
    license_plate character varying(20),
    assigned_technician_id character varying(50),
    status public.vehicle_status NOT NULL,
    odometer integer NOT NULL,
    last_service_odometer integer,
    last_service_at timestamp with time zone,
    service_interval_miles integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: vendor_bill_payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_bill_payments (
    id uuid NOT NULL,
    vendor_invoice_id uuid NOT NULL,
    amount numeric(12,2) NOT NULL,
    paid_date date,
    source character varying(20) NOT NULL,
    reference character varying(200),
    match_id uuid,
    statement_id uuid,
    created_by character varying(100),
    created_at timestamp with time zone NOT NULL,
    voided_at timestamp with time zone,
    voided_by character varying(100)
);


--
-- Name: vendor_invoice_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_invoice_lines (
    id uuid NOT NULL,
    vendor_invoice_id uuid NOT NULL,
    line_no integer,
    kind character varying(10) NOT NULL,
    item_label character varying(200),
    description character varying(500) NOT NULL,
    quantity numeric(12,2) NOT NULL,
    unit_cost numeric(12,4) NOT NULL,
    line_total numeric(12,2) NOT NULL,
    disposition character varying(20) NOT NULL,
    skip_reason text,
    job_id uuid,
    inventory_item_id uuid,
    expense_id uuid,
    job_part_needed_id character varying(36),
    stock_adjustment_id uuid,
    status character varying(20) NOT NULL,
    confirmed_by_user_id character varying(100),
    confirmed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: vendor_invoices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_invoices (
    id uuid NOT NULL,
    vendor_id uuid,
    vendor_key character varying(200) DEFAULT ''::character varying NOT NULL,
    vendor_name_raw character varying(200) NOT NULL,
    invoice_number character varying(60) NOT NULL,
    invoice_date date,
    po_reference character varying(200),
    terms character varying(60),
    due_date date,
    subtotal numeric(12,2) NOT NULL,
    tax numeric(12,2) NOT NULL,
    shipping numeric(12,2) NOT NULL,
    total numeric(12,2) NOT NULL,
    status character varying(20) NOT NULL,
    matched_job_id uuid,
    document_id uuid,
    source character varying(20) NOT NULL,
    extraction_method character varying(20) NOT NULL,
    possible_duplicate_of_id uuid,
    invariant_ok boolean,
    uploaded_by character varying(100),
    reviewed_at timestamp with time zone,
    reviewed_by_user_id character varying(100),
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: vendor_order_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_order_lines (
    id uuid NOT NULL,
    order_id uuid NOT NULL,
    line_no integer NOT NULL,
    description character varying(500),
    notes text,
    quantity numeric(12,2) NOT NULL,
    unit character varying(12),
    unit_cost numeric(12,4) NOT NULL,
    line_total numeric(12,2) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: vendor_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_orders (
    id uuid NOT NULL,
    vendor_name character varying(200) NOT NULL,
    vendor_code character varying(60),
    order_number character varying(60) NOT NULL,
    order_date date,
    salesperson character varying(60),
    ship_to character varying(200),
    terms character varying(200),
    customer_po character varying(200),
    lot_no character varying(120),
    called_in_by character varying(120),
    estimated_subtotal numeric(12,2) NOT NULL,
    estimated_shipping numeric(12,2) NOT NULL,
    estimated_tax numeric(12,2) NOT NULL,
    estimated_total numeric(12,2) NOT NULL,
    document_id uuid,
    parser_name character varying(60) NOT NULL,
    parser_version integer NOT NULL,
    line_count integer NOT NULL,
    matched_job_id uuid,
    job_confirmed_at timestamp with time zone,
    job_confirmed_by character varying(100),
    source character varying(20) DEFAULT 'email'::character varying NOT NULL,
    uploaded_by character varying(100),
    notes text,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: vendor_statement_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_statement_lines (
    id uuid NOT NULL,
    statement_id uuid NOT NULL,
    line_no integer NOT NULL,
    vendor_invoice_no character varying(60) NOT NULL,
    vendor_job_no character varying(60),
    line_date date,
    amount numeric(12,2) NOT NULL,
    balance numeric(12,2) NOT NULL,
    description character varying(500),
    po_ref character varying(120),
    aging_bucket character varying(20),
    classification character varying(20),
    matched_job_id uuid,
    notes text,
    raw_text text,
    raw_aging_json text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: vendor_statements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_statements (
    id uuid NOT NULL,
    vendor_name character varying(200) NOT NULL,
    vendor_code character varying(60),
    statement_date date,
    document_id uuid,
    parser_name character varying(60) NOT NULL,
    parser_version integer NOT NULL,
    raw_total numeric(12,2) NOT NULL,
    line_count integer NOT NULL,
    status character varying(20) NOT NULL,
    source character varying(20) DEFAULT 'upload'::character varying NOT NULL,
    uploaded_by character varying(100),
    notes text,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: vendors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendors (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    name_aliases text,
    account_number text,
    contact_name character varying(200),
    phone character varying(30),
    email character varying(200),
    website character varying(500),
    address text,
    city character varying(120),
    state character varying(20),
    zip character varying(20),
    notes text,
    payment_terms character varying(60),
    tax_id text,
    active boolean NOT NULL,
    qb_vendor_id character varying(120),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: warranties; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.warranties (
    id character varying(36) NOT NULL,
    job_id character varying(36) NOT NULL,
    customer_id character varying(36) NOT NULL,
    description text NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    terms text,
    status public.warranty_status NOT NULL,
    claim_count integer NOT NULL,
    last_claim_at timestamp with time zone,
    last_claim_notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: warranty_claims; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.warranty_claims (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    warranty_id character varying(36),
    job_id character varying(36),
    customer_id character varying(36) NOT NULL,
    serial_number character varying(120),
    manufacturer character varying(200),
    status character varying(30) NOT NULL,
    claim_notes text,
    filed_at timestamp with time zone,
    resolved_at timestamp with time zone,
    resolution text,
    created_by character varying(36) NOT NULL,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: webhook_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_deliveries (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    endpoint_id uuid,
    event_type character varying(100),
    payload json,
    idempotency_key character varying(100),
    attempt_count integer NOT NULL,
    last_attempt_at timestamp with time zone,
    next_retry_at timestamp with time zone,
    status public.webhook_delivery_status NOT NULL,
    response_status integer,
    created_at timestamp with time zone NOT NULL,
    delivered_at timestamp with time zone,
    duration_ms integer,
    error text,
    event character varying(100),
    request_body text,
    response_body text,
    subscription_id character varying(36),
    url character varying(500)
);


--
-- Name: webhook_delivery_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_delivery_logs (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    subscription_id uuid,
    event character varying(100),
    url character varying(2048),
    request_body text,
    response_status integer,
    response_body text,
    error text,
    duration_ms integer,
    delivered_at timestamp with time zone,
    attempt integer,
    created_at timestamp with time zone,
    delivery_status character varying(50),
    details text,
    request_id character varying(100),
    response_time_ms integer,
    status_code integer,
    tenant_id character varying(50),
    webhook_id character varying(36)
);


--
-- Name: webhook_endpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_endpoints (
    id uuid NOT NULL,
    url character varying(500) NOT NULL,
    secret text NOT NULL,
    events json NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: webhook_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_subscriptions (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    name character varying(200) NOT NULL,
    url character varying(2048) NOT NULL,
    secret character varying(200),
    events text NOT NULL,
    active boolean NOT NULL,
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: winback_campaigns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.winback_campaigns (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    name character varying(200) NOT NULL,
    status character varying(20) NOT NULL,
    channel character varying(20) NOT NULL,
    subject character varying(200),
    body_template text NOT NULL,
    inactivity_months integer NOT NULL,
    sent_at timestamp with time zone,
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: winback_sends; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.winback_sends (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    campaign_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    channel character varying(20) NOT NULL,
    status character varying(20) NOT NULL,
    error text,
    sent_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: workflow_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_rules (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    is_active boolean NOT NULL,
    trigger_event character varying(100) NOT NULL,
    conditions json NOT NULL,
    actions json NOT NULL,
    run_count integer NOT NULL,
    last_run_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: workflow_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_runs (
    id uuid NOT NULL,
    rule_id uuid NOT NULL,
    entity_type character varying(50) NOT NULL,
    entity_id character varying(50) NOT NULL,
    triggered_at timestamp with time zone NOT NULL,
    status public.workflow_run_status NOT NULL,
    actions_run json NOT NULL,
    error text
);


--
-- Name: ai_actions ai_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_actions
    ADD CONSTRAINT ai_actions_pkey PRIMARY KEY (id);


--
-- Name: ai_quote_log ai_quote_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_quote_log
    ADD CONSTRAINT ai_quote_log_pkey PRIMARY KEY (id);


--
-- Name: ai_usage_logs ai_usage_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage_logs
    ADD CONSTRAINT ai_usage_logs_pkey PRIMARY KEY (id);


--
-- Name: api_keys api_keys_key_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_key_hash_key UNIQUE (key_hash);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: app_settings app_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_pkey PRIMARY KEY (id);


--
-- Name: appointments appointments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.appointments
    ADD CONSTRAINT appointments_pkey PRIMARY KEY (id);


--
-- Name: audit_logs audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);


--
-- Name: automation_enrollments automation_enrollments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automation_enrollments
    ADD CONSTRAINT automation_enrollments_pkey PRIMARY KEY (id);


--
-- Name: automation_sequences automation_sequences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automation_sequences
    ADD CONSTRAINT automation_sequences_pkey PRIMARY KEY (id);


--
-- Name: automation_steps automation_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automation_steps
    ADD CONSTRAINT automation_steps_pkey PRIMARY KEY (id);


--
-- Name: bank_accounts bank_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_accounts
    ADD CONSTRAINT bank_accounts_pkey PRIMARY KEY (id);


--
-- Name: bank_feed_accounts bank_feed_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_accounts
    ADD CONSTRAINT bank_feed_accounts_pkey PRIMARY KEY (id);


--
-- Name: bank_feed_balance_snapshots bank_feed_balance_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_balance_snapshots
    ADD CONSTRAINT bank_feed_balance_snapshots_pkey PRIMARY KEY (id);


--
-- Name: bank_feed_documents bank_feed_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_documents
    ADD CONSTRAINT bank_feed_documents_pkey PRIMARY KEY (id);


--
-- Name: bank_feed_sync_schedule bank_feed_sync_schedule_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_sync_schedule
    ADD CONSTRAINT bank_feed_sync_schedule_pkey PRIMARY KEY (id);


--
-- Name: bank_feed_transactions bank_feed_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_transactions
    ADD CONSTRAINT bank_feed_transactions_pkey PRIMARY KEY (id);


--
-- Name: bank_match_externals bank_match_externals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_match_externals
    ADD CONSTRAINT bank_match_externals_pkey PRIMARY KEY (id);


--
-- Name: bank_match_lines bank_match_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_match_lines
    ADD CONSTRAINT bank_match_lines_pkey PRIMARY KEY (id);


--
-- Name: bank_matches bank_matches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_matches
    ADD CONSTRAINT bank_matches_pkey PRIMARY KEY (id);


--
-- Name: bank_statement_imports bank_statement_imports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_imports
    ADD CONSTRAINT bank_statement_imports_pkey PRIMARY KEY (id);


--
-- Name: bank_statement_line_images bank_statement_line_images_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_line_images
    ADD CONSTRAINT bank_statement_line_images_pkey PRIMARY KEY (id);


--
-- Name: bank_statement_line_sources bank_statement_line_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_line_sources
    ADD CONSTRAINT bank_statement_line_sources_pkey PRIMARY KEY (id);


--
-- Name: bank_statement_lines bank_statement_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_lines
    ADD CONSTRAINT bank_statement_lines_pkey PRIMARY KEY (id);


--
-- Name: banno_connections banno_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banno_connections
    ADD CONSTRAINT banno_connections_pkey PRIMARY KEY (id);


--
-- Name: banno_institutions banno_institutions_fi_host_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banno_institutions
    ADD CONSTRAINT banno_institutions_fi_host_key UNIQUE (fi_host);


--
-- Name: banno_institutions banno_institutions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banno_institutions
    ADD CONSTRAINT banno_institutions_pkey PRIMARY KEY (id);


--
-- Name: catalog_items catalog_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_items
    ADD CONSTRAINT catalog_items_pkey PRIMARY KEY (id);


--
-- Name: change_order_lines change_order_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.change_order_lines
    ADD CONSTRAINT change_order_lines_pkey PRIMARY KEY (id);


--
-- Name: change_orders change_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.change_orders
    ADD CONSTRAINT change_orders_pkey PRIMARY KEY (id);


--
-- Name: channel_analytics channel_analytics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_analytics
    ADD CONSTRAINT channel_analytics_pkey PRIMARY KEY (id);


--
-- Name: checklist_items checklist_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_items
    ADD CONSTRAINT checklist_items_pkey PRIMARY KEY (id);


--
-- Name: checklist_templates checklist_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_templates
    ADD CONSTRAINT checklist_templates_pkey PRIMARY KEY (id);


--
-- Name: checklists checklists_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklists
    ADD CONSTRAINT checklists_pkey PRIMARY KEY (id);


--
-- Name: chi_door_catalog chi_door_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chi_door_catalog
    ADD CONSTRAINT chi_door_catalog_pkey PRIMARY KEY (id);


--
-- Name: chi_parts_catalog chi_parts_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chi_parts_catalog
    ADD CONSTRAINT chi_parts_catalog_pkey PRIMARY KEY (id);


--
-- Name: client_errors client_errors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.client_errors
    ADD CONSTRAINT client_errors_pkey PRIMARY KEY (id);


--
-- Name: commission_entries commission_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.commission_entries
    ADD CONSTRAINT commission_entries_pkey PRIMARY KEY (id);


--
-- Name: commission_rules commission_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.commission_rules
    ADD CONSTRAINT commission_rules_pkey PRIMARY KEY (id);


--
-- Name: companies companies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.companies
    ADD CONSTRAINT companies_pkey PRIMARY KEY (id);


--
-- Name: company_module_grants company_module_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_module_grants
    ADD CONSTRAINT company_module_grants_pkey PRIMARY KEY (id);


--
-- Name: custom_catalog_items custom_catalog_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.custom_catalog_items
    ADD CONSTRAINT custom_catalog_items_pkey PRIMARY KEY (id);


--
-- Name: custom_catalogs custom_catalogs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.custom_catalogs
    ADD CONSTRAINT custom_catalogs_pkey PRIMARY KEY (id);


--
-- Name: custom_field_definitions custom_field_definitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.custom_field_definitions
    ADD CONSTRAINT custom_field_definitions_pkey PRIMARY KEY (id);


--
-- Name: custom_field_values custom_field_values_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.custom_field_values
    ADD CONSTRAINT custom_field_values_pkey PRIMARY KEY (id);


--
-- Name: customer_contacts customer_contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_contacts
    ADD CONSTRAINT customer_contacts_pkey PRIMARY KEY (id);


--
-- Name: customer_equipments customer_equipments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_equipments
    ADD CONSTRAINT customer_equipments_pkey PRIMARY KEY (id);


--
-- Name: customer_locations customer_locations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_locations
    ADD CONSTRAINT customer_locations_pkey PRIMARY KEY (id);


--
-- Name: customer_reviews customer_reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_reviews
    ADD CONSTRAINT customer_reviews_pkey PRIMARY KEY (id);


--
-- Name: customer_users customer_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_users
    ADD CONSTRAINT customer_users_pkey PRIMARY KEY (id);


--
-- Name: customer_volume_discount_tiers customer_volume_discount_tiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_volume_discount_tiers
    ADD CONSTRAINT customer_volume_discount_tiers_pkey PRIMARY KEY (id);


--
-- Name: customers customers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_pkey PRIMARY KEY (id);


--
-- Name: dealer_orders dealer_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dealer_orders
    ADD CONSTRAINT dealer_orders_pkey PRIMARY KEY (id);


--
-- Name: device_alert_registrations device_alert_registrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_alert_registrations
    ADD CONSTRAINT device_alert_registrations_pkey PRIMARY KEY (id);


--
-- Name: device_alert_registrations device_alert_registrations_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_alert_registrations
    ADD CONSTRAINT device_alert_registrations_token_hash_key UNIQUE (token_hash);


--
-- Name: device_tokens device_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_tokens
    ADD CONSTRAINT device_tokens_pkey PRIMARY KEY (id);


--
-- Name: device_tokens device_tokens_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_tokens
    ADD CONSTRAINT device_tokens_token_key UNIQUE (token);


--
-- Name: dispatch_routes dispatch_routes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dispatch_routes
    ADD CONSTRAINT dispatch_routes_pkey PRIMARY KEY (id);


--
-- Name: distributor_analytics distributor_analytics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.distributor_analytics
    ADD CONSTRAINT distributor_analytics_pkey PRIMARY KEY (id);


--
-- Name: document_folders document_folders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_folders
    ADD CONSTRAINT document_folders_pkey PRIMARY KEY (id);


--
-- Name: document_signatures document_signatures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_signatures
    ADD CONSTRAINT document_signatures_pkey PRIMARY KEY (id);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);


--
-- Name: door_listing_photos door_listing_photos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.door_listing_photos
    ADD CONSTRAINT door_listing_photos_pkey PRIMARY KEY (id);


--
-- Name: door_listings door_listings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.door_listings
    ADD CONSTRAINT door_listings_pkey PRIMARY KEY (id);


--
-- Name: door_specs door_specs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.door_specs
    ADD CONSTRAINT door_specs_pkey PRIMARY KEY (catalog_item_id);


--
-- Name: email_settings email_settings_company_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_settings
    ADD CONSTRAINT email_settings_company_id_key UNIQUE (company_id);


--
-- Name: email_settings email_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_settings
    ADD CONSTRAINT email_settings_pkey PRIMARY KEY (id);


--
-- Name: equipment_asset_history equipment_asset_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.equipment_asset_history
    ADD CONSTRAINT equipment_asset_history_pkey PRIMARY KEY (id);


--
-- Name: equipment_assets equipment_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.equipment_assets
    ADD CONSTRAINT equipment_assets_pkey PRIMARY KEY (id);


--
-- Name: equipment_service_history equipment_service_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.equipment_service_history
    ADD CONSTRAINT equipment_service_history_pkey PRIMARY KEY (id);


--
-- Name: estimate_lines estimate_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimate_lines
    ADD CONSTRAINT estimate_lines_pkey PRIMARY KEY (id);


--
-- Name: estimate_nurture_log estimate_nurture_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimate_nurture_log
    ADD CONSTRAINT estimate_nurture_log_pkey PRIMARY KEY (id);


--
-- Name: estimate_nurture_rules estimate_nurture_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimate_nurture_rules
    ADD CONSTRAINT estimate_nurture_rules_pkey PRIMARY KEY (id);


--
-- Name: estimates estimates_estimate_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimates
    ADD CONSTRAINT estimates_estimate_number_key UNIQUE (estimate_number);


--
-- Name: estimates estimates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimates
    ADD CONSTRAINT estimates_pkey PRIMARY KEY (id);


--
-- Name: estimates estimates_public_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimates
    ADD CONSTRAINT estimates_public_token_key UNIQUE (public_token);


--
-- Name: expense_lines expense_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_lines
    ADD CONSTRAINT expense_lines_pkey PRIMARY KEY (id);


--
-- Name: expense_receipts expense_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_receipts
    ADD CONSTRAINT expense_receipts_pkey PRIMARY KEY (id);


--
-- Name: expenses expenses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_pkey PRIMARY KEY (id);


--
-- Name: fleet_vehicle_service_logs_router fleet_vehicle_service_logs_router_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fleet_vehicle_service_logs_router
    ADD CONSTRAINT fleet_vehicle_service_logs_router_pkey PRIMARY KEY (id);


--
-- Name: fleet_vehicles_router fleet_vehicles_router_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fleet_vehicles_router
    ADD CONSTRAINT fleet_vehicles_router_pkey PRIMARY KEY (id);


--
-- Name: follow_ups follow_ups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.follow_ups
    ADD CONSTRAINT follow_ups_pkey PRIMARY KEY (id);


--
-- Name: forecast_snapshot_invoices forecast_snapshot_invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.forecast_snapshot_invoices
    ADD CONSTRAINT forecast_snapshot_invoices_pkey PRIMARY KEY (id);


--
-- Name: forecast_snapshots forecast_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.forecast_snapshots
    ADD CONSTRAINT forecast_snapshots_pkey PRIMARY KEY (id);


--
-- Name: gdpr_data_access_logs gdpr_data_access_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gdpr_data_access_logs
    ADD CONSTRAINT gdpr_data_access_logs_pkey PRIMARY KEY (id);


--
-- Name: gl_accounts gl_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_accounts
    ADD CONSTRAINT gl_accounts_pkey PRIMARY KEY (id);


--
-- Name: gl_journal_entries gl_journal_entries_entry_no_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_journal_entries
    ADD CONSTRAINT gl_journal_entries_entry_no_key UNIQUE (entry_no);


--
-- Name: gl_journal_entries gl_journal_entries_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_journal_entries
    ADD CONSTRAINT gl_journal_entries_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: gl_journal_entries gl_journal_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_journal_entries
    ADD CONSTRAINT gl_journal_entries_pkey PRIMARY KEY (id);


--
-- Name: gl_journal_lines gl_journal_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_journal_lines
    ADD CONSTRAINT gl_journal_lines_pkey PRIMARY KEY (id);


--
-- Name: gl_period_locks gl_period_locks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_period_locks
    ADD CONSTRAINT gl_period_locks_pkey PRIMARY KEY (id);


--
-- Name: gl_settings gl_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_settings
    ADD CONSTRAINT gl_settings_pkey PRIMARY KEY (id);


--
-- Name: holding_areas holding_areas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holding_areas
    ADD CONSTRAINT holding_areas_pkey PRIMARY KEY (id);


--
-- Name: hubx_door_orders hubx_door_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hubx_door_orders
    ADD CONSTRAINT hubx_door_orders_pkey PRIMARY KEY (id);


--
-- Name: hubx_order_emails hubx_order_emails_message_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hubx_order_emails
    ADD CONSTRAINT hubx_order_emails_message_key_key UNIQUE (message_key);


--
-- Name: hubx_order_emails hubx_order_emails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hubx_order_emails
    ADD CONSTRAINT hubx_order_emails_pkey PRIMARY KEY (id);


--
-- Name: inbound_emails inbound_emails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbound_emails
    ADD CONSTRAINT inbound_emails_pkey PRIMARY KEY (id);


--
-- Name: integration_configs integration_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_configs
    ADD CONSTRAINT integration_configs_pkey PRIMARY KEY (id);


--
-- Name: internal_tasks internal_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_tasks
    ADD CONSTRAINT internal_tasks_pkey PRIMARY KEY (id);


--
-- Name: inventory_items inventory_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_items
    ADD CONSTRAINT inventory_items_pkey PRIMARY KEY (id);


--
-- Name: invoice_adjustments invoice_adjustments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_adjustments
    ADD CONSTRAINT invoice_adjustments_pkey PRIMARY KEY (id);


--
-- Name: invoice_lines invoice_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_pkey PRIMARY KEY (id);


--
-- Name: invoices invoices_invoice_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_invoice_number_key UNIQUE (invoice_number);


--
-- Name: invoices invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_pkey PRIMARY KEY (id);


--
-- Name: invoices invoices_public_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_public_token_key UNIQUE (public_token);


--
-- Name: job_assignments job_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_assignments
    ADD CONSTRAINT job_assignments_pkey PRIMARY KEY (id);


--
-- Name: job_chat_messages job_chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_chat_messages
    ADD CONSTRAINT job_chat_messages_pkey PRIMARY KEY (id);


--
-- Name: job_closeouts job_closeouts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_closeouts
    ADD CONSTRAINT job_closeouts_pkey PRIMARY KEY (id);


--
-- Name: job_dependencies job_dependencies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_dependencies
    ADD CONSTRAINT job_dependencies_pkey PRIMARY KEY (id);


--
-- Name: job_diagnoses job_diagnoses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_diagnoses
    ADD CONSTRAINT job_diagnoses_pkey PRIMARY KEY (id);


--
-- Name: job_hazards job_hazards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_hazards
    ADD CONSTRAINT job_hazards_pkey PRIMARY KEY (id);


--
-- Name: job_notes job_notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_notes
    ADD CONSTRAINT job_notes_pkey PRIMARY KEY (id);


--
-- Name: job_parts_needed job_parts_needed_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_parts_needed
    ADD CONSTRAINT job_parts_needed_pkey PRIMARY KEY (id);


--
-- Name: job_parts job_parts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_parts
    ADD CONSTRAINT job_parts_pkey PRIMARY KEY (id);


--
-- Name: job_photos job_photos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_photos
    ADD CONSTRAINT job_photos_pkey PRIMARY KEY (id);


--
-- Name: job_receipts job_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_receipts
    ADD CONSTRAINT job_receipts_pkey PRIMARY KEY (id);


--
-- Name: job_templates job_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_templates
    ADD CONSTRAINT job_templates_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: labor_price_items labor_price_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.labor_price_items
    ADD CONSTRAINT labor_price_items_pkey PRIMARY KEY (id);


--
-- Name: landing_leads landing_leads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.landing_leads
    ADD CONSTRAINT landing_leads_pkey PRIMARY KEY (id);


--
-- Name: leads leads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_pkey PRIMARY KEY (id);


--
-- Name: loyalty_points loyalty_points_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loyalty_points
    ADD CONSTRAINT loyalty_points_pkey PRIMARY KEY (id);


--
-- Name: loyalty_referrals loyalty_referrals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loyalty_referrals
    ADD CONSTRAINT loyalty_referrals_pkey PRIMARY KEY (id);


--
-- Name: loyalty_tiers loyalty_tiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loyalty_tiers
    ADD CONSTRAINT loyalty_tiers_pkey PRIMARY KEY (id);


--
-- Name: maintenance_plans maintenance_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.maintenance_plans
    ADD CONSTRAINT maintenance_plans_pkey PRIMARY KEY (id);


--
-- Name: margin_tiers margin_tiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.margin_tiers
    ADD CONSTRAINT margin_tiers_pkey PRIMARY KEY (id);


--
-- Name: marketing_campaigns marketing_campaigns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_campaigns
    ADD CONSTRAINT marketing_campaigns_pkey PRIMARY KEY (id);


--
-- Name: markup_rules markup_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.markup_rules
    ADD CONSTRAINT markup_rules_pkey PRIMARY KEY (id);


--
-- Name: message_thread_members message_thread_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_thread_members
    ADD CONSTRAINT message_thread_members_pkey PRIMARY KEY (thread_id, user_id);


--
-- Name: message_threads message_threads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_threads
    ADD CONSTRAINT message_threads_pkey PRIMARY KEY (id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- Name: monthly_budgets monthly_budgets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.monthly_budgets
    ADD CONSTRAINT monthly_budgets_pkey PRIMARY KEY (id);


--
-- Name: next_actions next_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.next_actions
    ADD CONSTRAINT next_actions_pkey PRIMARY KEY (id);


--
-- Name: notification_log notification_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_log
    ADD CONSTRAINT notification_log_pkey PRIMARY KEY (id);


--
-- Name: notification_preferences notification_preferences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_preferences
    ADD CONSTRAINT notification_preferences_pkey PRIMARY KEY (id);


--
-- Name: notification_sent_history notification_sent_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_sent_history
    ADD CONSTRAINT notification_sent_history_pkey PRIMARY KEY (id);


--
-- Name: notification_templates notification_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_templates
    ADD CONSTRAINT notification_templates_pkey PRIMARY KEY (id);


--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);


--
-- Name: notifications_settings notifications_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications_settings
    ADD CONSTRAINT notifications_settings_pkey PRIMARY KEY (tenant_id);


--
-- Name: onboarding_state onboarding_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding_state
    ADD CONSTRAINT onboarding_state_pkey PRIMARY KEY (id);


--
-- Name: outbound_emails outbound_emails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbound_emails
    ADD CONSTRAINT outbound_emails_pkey PRIMARY KEY (id);


--
-- Name: outlook_accounts outlook_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_accounts
    ADD CONSTRAINT outlook_accounts_pkey PRIMARY KEY (id);


--
-- Name: outlook_attachments outlook_attachments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_attachments
    ADD CONSTRAINT outlook_attachments_pkey PRIMARY KEY (id);


--
-- Name: outlook_folder_prefs outlook_folder_prefs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_folder_prefs
    ADD CONSTRAINT outlook_folder_prefs_pkey PRIMARY KEY (id);


--
-- Name: outlook_folder_sync_state outlook_folder_sync_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_folder_sync_state
    ADD CONSTRAINT outlook_folder_sync_state_pkey PRIMARY KEY (id);


--
-- Name: outlook_folders outlook_folders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_folders
    ADD CONSTRAINT outlook_folders_pkey PRIMARY KEY (id);


--
-- Name: outlook_messages outlook_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_messages
    ADD CONSTRAINT outlook_messages_pkey PRIMARY KEY (id);


--
-- Name: outlook_settings outlook_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_settings
    ADD CONSTRAINT outlook_settings_pkey PRIMARY KEY (id);


--
-- Name: outlook_subscriptions outlook_subscriptions_account_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_subscriptions
    ADD CONSTRAINT outlook_subscriptions_account_id_key UNIQUE (account_id);


--
-- Name: outlook_subscriptions outlook_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_subscriptions
    ADD CONSTRAINT outlook_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: overhead_obligations overhead_obligations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.overhead_obligations
    ADD CONSTRAINT overhead_obligations_pkey PRIMARY KEY (id);


--
-- Name: part_prices part_prices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.part_prices
    ADD CONSTRAINT part_prices_pkey PRIMARY KEY (id);


--
-- Name: parts parts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parts
    ADD CONSTRAINT parts_pkey PRIMARY KEY (id);


--
-- Name: parts parts_sku_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parts
    ADD CONSTRAINT parts_sku_key UNIQUE (sku);


--
-- Name: payment_plan_installments payment_plan_installments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_plan_installments
    ADD CONSTRAINT payment_plan_installments_pkey PRIMARY KEY (id);


--
-- Name: payment_plans payment_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_plans
    ADD CONSTRAINT payment_plans_pkey PRIMARY KEY (id);


--
-- Name: payment_reminders payment_reminders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_reminders
    ADD CONSTRAINT payment_reminders_pkey PRIMARY KEY (id);


--
-- Name: payments payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (id);


--
-- Name: payroll_entries payroll_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_entries
    ADD CONSTRAINT payroll_entries_pkey PRIMARY KEY (id);


--
-- Name: pdf_templates pdf_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pdf_templates
    ADD CONSTRAINT pdf_templates_pkey PRIMARY KEY (id);


--
-- Name: performance_slow_events performance_slow_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.performance_slow_events
    ADD CONSTRAINT performance_slow_events_pkey PRIMARY KEY (id);


--
-- Name: phone_com_calls phone_com_calls_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_calls
    ADD CONSTRAINT phone_com_calls_pkey PRIMARY KEY (id);


--
-- Name: phone_com_contact_push phone_com_contact_push_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_contact_push
    ADD CONSTRAINT phone_com_contact_push_pkey PRIMARY KEY (id);


--
-- Name: phone_com_extensions phone_com_extensions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_extensions
    ADD CONSTRAINT phone_com_extensions_pkey PRIMARY KEY (id);


--
-- Name: phone_com_faxes phone_com_faxes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_faxes
    ADD CONSTRAINT phone_com_faxes_pkey PRIMARY KEY (id);


--
-- Name: phone_com_messages phone_com_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_messages
    ADD CONSTRAINT phone_com_messages_pkey PRIMARY KEY (id);


--
-- Name: phone_com_numbers phone_com_numbers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_numbers
    ADD CONSTRAINT phone_com_numbers_pkey PRIMARY KEY (id);


--
-- Name: phone_com_stats_daily phone_com_stats_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_stats_daily
    ADD CONSTRAINT phone_com_stats_daily_pkey PRIMARY KEY (id);


--
-- Name: phone_com_voicemails phone_com_voicemails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_voicemails
    ADD CONSTRAINT phone_com_voicemails_pkey PRIMARY KEY (id);


--
-- Name: plan_enrollments plan_enrollments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_enrollments
    ADD CONSTRAINT plan_enrollments_pkey PRIMARY KEY (id);


--
-- Name: plan_steps plan_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_steps
    ADD CONSTRAINT plan_steps_pkey PRIMARY KEY (id);


--
-- Name: planner_day_notes planner_day_notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.planner_day_notes
    ADD CONSTRAINT planner_day_notes_pkey PRIMARY KEY (id);


--
-- Name: planner_tasks planner_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.planner_tasks
    ADD CONSTRAINT planner_tasks_pkey PRIMARY KEY (id);


--
-- Name: plans plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plans
    ADD CONSTRAINT plans_pkey PRIMARY KEY (id);


--
-- Name: plugin_email_outbox plugin_email_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plugin_email_outbox
    ADD CONSTRAINT plugin_email_outbox_pkey PRIMARY KEY (id);


--
-- Name: portal_messages portal_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_messages
    ADD CONSTRAINT portal_messages_pkey PRIMARY KEY (id);


--
-- Name: pricing_class_settings pricing_class_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pricing_class_settings
    ADD CONSTRAINT pricing_class_settings_pkey PRIMARY KEY (pricing_class);


--
-- Name: pricing_settings pricing_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pricing_settings
    ADD CONSTRAINT pricing_settings_pkey PRIMARY KEY (id);


--
-- Name: pricing_tier_sets pricing_tier_sets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pricing_tier_sets
    ADD CONSTRAINT pricing_tier_sets_pkey PRIMARY KEY (id);


--
-- Name: pricing_tiers pricing_tiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pricing_tiers
    ADD CONSTRAINT pricing_tiers_pkey PRIMARY KEY (id);


--
-- Name: proposal_tier_lines proposal_tier_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposal_tier_lines
    ADD CONSTRAINT proposal_tier_lines_pkey PRIMARY KEY (id);


--
-- Name: proposal_tiers proposal_tiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposal_tiers
    ADD CONSTRAINT proposal_tiers_pkey PRIMARY KEY (id);


--
-- Name: purchase_order_lines purchase_order_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order_lines
    ADD CONSTRAINT purchase_order_lines_pkey PRIMARY KEY (id);


--
-- Name: purchase_orders purchase_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_orders
    ADD CONSTRAINT purchase_orders_pkey PRIMARY KEY (id);


--
-- Name: push_subscriptions push_subscriptions_endpoint_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.push_subscriptions
    ADD CONSTRAINT push_subscriptions_endpoint_key UNIQUE (endpoint);


--
-- Name: push_subscriptions push_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.push_subscriptions
    ADD CONSTRAINT push_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: qb_bank_transactions qb_bank_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_bank_transactions
    ADD CONSTRAINT qb_bank_transactions_pkey PRIMARY KEY (id);


--
-- Name: qb_banking_entries qb_banking_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_banking_entries
    ADD CONSTRAINT qb_banking_entries_pkey PRIMARY KEY (id);


--
-- Name: qb_connections qb_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_connections
    ADD CONSTRAINT qb_connections_pkey PRIMARY KEY (id);


--
-- Name: qb_deposits qb_deposits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_deposits
    ADD CONSTRAINT qb_deposits_pkey PRIMARY KEY (id);


--
-- Name: qb_entity_maps qb_entity_maps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_entity_maps
    ADD CONSTRAINT qb_entity_maps_pkey PRIMARY KEY (id);


--
-- Name: qb_pnl_monthly qb_pnl_monthly_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_pnl_monthly
    ADD CONSTRAINT qb_pnl_monthly_pkey PRIMARY KEY (id);


--
-- Name: qb_recurring_transactions qb_recurring_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_recurring_transactions
    ADD CONSTRAINT qb_recurring_transactions_pkey PRIMARY KEY (id);


--
-- Name: qb_sync_schedule qb_sync_schedule_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_sync_schedule
    ADD CONSTRAINT qb_sync_schedule_pkey PRIMARY KEY (id);


--
-- Name: qb_token_store qb_token_store_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_token_store
    ADD CONSTRAINT qb_token_store_pkey PRIMARY KEY (id);


--
-- Name: qb_transfers qb_transfers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_transfers
    ADD CONSTRAINT qb_transfers_pkey PRIMARY KEY (id);


--
-- Name: qb_vendors qb_vendors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_vendors
    ADD CONSTRAINT qb_vendors_pkey PRIMARY KEY (id);


--
-- Name: qb_webhook_events qb_webhook_events_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_webhook_events
    ADD CONSTRAINT qb_webhook_events_event_id_key UNIQUE (event_id);


--
-- Name: qb_webhook_events qb_webhook_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_webhook_events
    ADD CONSTRAINT qb_webhook_events_pkey PRIMARY KEY (id);


--
-- Name: quote_request_photos quote_request_photos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quote_request_photos
    ADD CONSTRAINT quote_request_photos_pkey PRIMARY KEY (id);


--
-- Name: quote_requests quote_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quote_requests
    ADD CONSTRAINT quote_requests_pkey PRIMARY KEY (id);


--
-- Name: quote_templates quote_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quote_templates
    ADD CONSTRAINT quote_templates_pkey PRIMARY KEY (id);


--
-- Name: recurring_job_schedules recurring_job_schedules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recurring_job_schedules
    ADD CONSTRAINT recurring_job_schedules_pkey PRIMARY KEY (id);


--
-- Name: recurring_stream_hits recurring_stream_hits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recurring_stream_hits
    ADD CONSTRAINT recurring_stream_hits_pkey PRIMARY KEY (id);


--
-- Name: recurring_streams recurring_streams_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recurring_streams
    ADD CONSTRAINT recurring_streams_pkey PRIMARY KEY (id);


--
-- Name: reminder_settings reminder_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reminder_settings
    ADD CONSTRAINT reminder_settings_pkey PRIMARY KEY (id);


--
-- Name: resale_quotes resale_quotes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resale_quotes
    ADD CONSTRAINT resale_quotes_pkey PRIMARY KEY (id);


--
-- Name: reseller_profiles reseller_profiles_customer_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reseller_profiles
    ADD CONSTRAINT reseller_profiles_customer_id_key UNIQUE (customer_id);


--
-- Name: reseller_profiles reseller_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reseller_profiles
    ADD CONSTRAINT reseller_profiles_pkey PRIMARY KEY (id);


--
-- Name: resources resources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resources
    ADD CONSTRAINT resources_pkey PRIMARY KEY (id);


--
-- Name: role_permissions role_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (role);


--
-- Name: saved_reports saved_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_reports
    ADD CONSTRAINT saved_reports_pkey PRIMARY KEY (id);


--
-- Name: scheduled_sms scheduled_sms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_sms
    ADD CONSTRAINT scheduled_sms_pkey PRIMARY KEY (id);


--
-- Name: security_events security_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_events
    ADD CONSTRAINT security_events_pkey PRIMARY KEY (id);


--
-- Name: segments segments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.segments
    ADD CONSTRAINT segments_pkey PRIMARY KEY (id);


--
-- Name: service_agreement_templates service_agreement_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_agreement_templates
    ADD CONSTRAINT service_agreement_templates_pkey PRIMARY KEY (id);


--
-- Name: service_agreements service_agreements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_agreements
    ADD CONSTRAINT service_agreements_pkey PRIMARY KEY (id);


--
-- Name: service_locations service_locations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_locations
    ADD CONSTRAINT service_locations_pkey PRIMARY KEY (id);


--
-- Name: service_triggers service_triggers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_triggers
    ADD CONSTRAINT service_triggers_pkey PRIMARY KEY (id);


--
-- Name: stock_adjustments stock_adjustments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustments
    ADD CONSTRAINT stock_adjustments_pkey PRIMARY KEY (id);


--
-- Name: stripe_webhook_events stripe_webhook_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stripe_webhook_events
    ADD CONSTRAINT stripe_webhook_events_pkey PRIMARY KEY (event_id);


--
-- Name: support_tickets support_tickets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_pkey PRIMARY KEY (id);


--
-- Name: survey_responses survey_responses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_responses
    ADD CONSTRAINT survey_responses_pkey PRIMARY KEY (id);


--
-- Name: survey_sends survey_sends_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_sends
    ADD CONSTRAINT survey_sends_pkey PRIMARY KEY (id);


--
-- Name: survey_templates survey_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_templates
    ADD CONSTRAINT survey_templates_pkey PRIMARY KEY (id);


--
-- Name: tag_assignments tag_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_assignments
    ADD CONSTRAINT tag_assignments_pkey PRIMARY KEY (id);


--
-- Name: tags tags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT tags_pkey PRIMARY KEY (id);


--
-- Name: tax_config tax_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_config
    ADD CONSTRAINT tax_config_pkey PRIMARY KEY (id);


--
-- Name: tax_exemption tax_exemption_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_exemption
    ADD CONSTRAINT tax_exemption_pkey PRIMARY KEY (id);


--
-- Name: tax_jurisdictions tax_jurisdictions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_jurisdictions
    ADD CONSTRAINT tax_jurisdictions_pkey PRIMARY KEY (id);


--
-- Name: team_message_recipients team_message_recipients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_message_recipients
    ADD CONSTRAINT team_message_recipients_pkey PRIMARY KEY (id);


--
-- Name: team_messages team_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_messages
    ADD CONSTRAINT team_messages_pkey PRIMARY KEY (id);


--
-- Name: tech_commission_rates tech_commission_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tech_commission_rates
    ADD CONSTRAINT tech_commission_rates_pkey PRIMARY KEY (id);


--
-- Name: tech_locations tech_locations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tech_locations
    ADD CONSTRAINT tech_locations_pkey PRIMARY KEY (id);


--
-- Name: technician_locations technician_locations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.technician_locations
    ADD CONSTRAINT technician_locations_pkey PRIMARY KEY (id);


--
-- Name: technicians technicians_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.technicians
    ADD CONSTRAINT technicians_pkey PRIMARY KEY (id);


--
-- Name: tenant_forecast_settings tenant_forecast_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_forecast_settings
    ADD CONSTRAINT tenant_forecast_settings_pkey PRIMARY KEY (id);


--
-- Name: tenant_roles tenant_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_roles
    ADD CONSTRAINT tenant_roles_pkey PRIMARY KEY (id);


--
-- Name: time_entries time_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_pkey PRIMARY KEY (id);


--
-- Name: time_off_requests time_off_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_off_requests
    ADD CONSTRAINT time_off_requests_pkey PRIMARY KEY (id);


--
-- Name: timeclock_breaks_router timeclock_breaks_router_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.timeclock_breaks_router
    ADD CONSTRAINT timeclock_breaks_router_pkey PRIMARY KEY (id);


--
-- Name: timeclock_entries_router timeclock_entries_router_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.timeclock_entries_router
    ADD CONSTRAINT timeclock_entries_router_pkey PRIMARY KEY (id);


--
-- Name: timeclocks timeclocks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.timeclocks
    ADD CONSTRAINT timeclocks_pkey PRIMARY KEY (id);


--
-- Name: bank_accounts uq_bank_accounts_inst_last4; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_accounts
    ADD CONSTRAINT uq_bank_accounts_inst_last4 UNIQUE (institution, last4);


--
-- Name: bank_feed_accounts uq_bank_feed_acct_conn_ext; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_accounts
    ADD CONSTRAINT uq_bank_feed_acct_conn_ext UNIQUE (connection_id, external_account_id);


--
-- Name: bank_feed_balance_snapshots uq_bank_feed_balsnap_acct_date; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_balance_snapshots
    ADD CONSTRAINT uq_bank_feed_balsnap_acct_date UNIQUE (account_id, snapshot_date);


--
-- Name: bank_feed_documents uq_bank_feed_doc_conn_ext; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_documents
    ADD CONSTRAINT uq_bank_feed_doc_conn_ext UNIQUE (connection_id, external_document_id);


--
-- Name: bank_feed_transactions uq_bank_feed_txn_acct_ext; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_transactions
    ADD CONSTRAINT uq_bank_feed_txn_acct_ext UNIQUE (account_id, external_transaction_id);


--
-- Name: bank_statement_lines uq_bank_stmt_line_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_lines
    ADD CONSTRAINT uq_bank_stmt_line_hash UNIQUE (bank_account_id, line_hash);


--
-- Name: bank_statement_line_sources uq_bank_stmt_line_source; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_line_sources
    ADD CONSTRAINT uq_bank_stmt_line_source UNIQUE (line_id, import_id);


--
-- Name: banno_connections uq_banno_conn_inst_user; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banno_connections
    ADD CONSTRAINT uq_banno_conn_inst_user UNIQUE (institution_id, banno_user_id);


--
-- Name: company_module_grants uq_company_module_grant; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_module_grants
    ADD CONSTRAINT uq_company_module_grant UNIQUE (company_id, module_key);


--
-- Name: custom_field_definitions uq_custom_field_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.custom_field_definitions
    ADD CONSTRAINT uq_custom_field_key UNIQUE (company_id, entity_type, field_key);


--
-- Name: custom_field_values uq_custom_field_value; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.custom_field_values
    ADD CONSTRAINT uq_custom_field_value UNIQUE (company_id, definition_id, entity_id);


--
-- Name: outlook_messages uq_email_account_graph_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_messages
    ADD CONSTRAINT uq_email_account_graph_id UNIQUE (account_id, graph_message_id);


--
-- Name: forecast_snapshot_invoices uq_forecast_snapshot_invoice; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.forecast_snapshot_invoices
    ADD CONSTRAINT uq_forecast_snapshot_invoice UNIQUE (snapshot_id, invoice_id);


--
-- Name: gl_settings uq_gl_settings_company; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_settings
    ADD CONSTRAINT uq_gl_settings_company UNIQUE (company_id);


--
-- Name: markup_rules uq_markup_rule_category; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.markup_rules
    ADD CONSTRAINT uq_markup_rule_category UNIQUE (company_id, category);


--
-- Name: team_message_recipients uq_message_recipient; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_message_recipients
    ADD CONSTRAINT uq_message_recipient UNIQUE (company_id, message_id, recipient_id);


--
-- Name: monthly_budgets uq_monthly_budget_year_month_account; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.monthly_budgets
    ADD CONSTRAINT uq_monthly_budget_year_month_account UNIQUE (year, month, qb_account_id);


--
-- Name: outlook_folders uq_outlook_folder_account_graph_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_folders
    ADD CONSTRAINT uq_outlook_folder_account_graph_id UNIQUE (account_id, graph_folder_id);


--
-- Name: outlook_folder_prefs uq_outlook_folder_prefs_account_folder; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_folder_prefs
    ADD CONSTRAINT uq_outlook_folder_prefs_account_folder UNIQUE (account_id, folder_id);


--
-- Name: outlook_folder_sync_state uq_outlook_folder_sync_state_account_folder; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_folder_sync_state
    ADD CONSTRAINT uq_outlook_folder_sync_state_account_folder UNIQUE (account_id, folder_id);


--
-- Name: pdf_templates uq_pdf_template; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pdf_templates
    ADD CONSTRAINT uq_pdf_template UNIQUE (company_id, template_type);


--
-- Name: planner_day_notes uq_planner_day_notes_user_date; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.planner_day_notes
    ADD CONSTRAINT uq_planner_day_notes_user_date UNIQUE (user_id, note_date);


--
-- Name: pricing_tier_sets uq_pricing_tier_set_cat_class; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pricing_tier_sets
    ADD CONSTRAINT uq_pricing_tier_set_cat_class UNIQUE (pricing_category, pricing_class);


--
-- Name: qb_entity_maps uq_qb_map_local; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_entity_maps
    ADD CONSTRAINT uq_qb_map_local UNIQUE (tenant_id, entity_type, local_id);


--
-- Name: qb_entity_maps uq_qb_map_remote; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_entity_maps
    ADD CONSTRAINT uq_qb_map_remote UNIQUE (tenant_id, entity_type, qb_id);


--
-- Name: qb_pnl_monthly uq_qb_pnl_monthly_year_month_account; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_pnl_monthly
    ADD CONSTRAINT uq_qb_pnl_monthly_year_month_account UNIQUE (year, month, qb_account_id);


--
-- Name: qb_token_store uq_qb_token_tenant_realm; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_token_store
    ADD CONSTRAINT uq_qb_token_tenant_realm UNIQUE (tenant_id, realm_id);


--
-- Name: qb_vendors uq_qb_vendor_tenant; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_vendors
    ADD CONSTRAINT uq_qb_vendor_tenant UNIQUE (tenant_id, qb_vendor_id);


--
-- Name: recurring_stream_hits uq_recurring_stream_hit_txn; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recurring_stream_hits
    ADD CONSTRAINT uq_recurring_stream_hit_txn UNIQUE (stream_id, qb_txn_id);


--
-- Name: tag_assignments uq_tag_assignment; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_assignments
    ADD CONSTRAINT uq_tag_assignment UNIQUE (company_id, tag_id, entity_type, entity_id);


--
-- Name: tags uq_tag_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT uq_tag_name UNIQUE (company_id, name);


--
-- Name: tenant_roles uq_tenant_role_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_roles
    ADD CONSTRAINT uq_tenant_role_name UNIQUE (company_id, name);


--
-- Name: user_locations uq_user_locations_user_location; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_locations
    ADD CONSTRAINT uq_user_locations_user_location UNIQUE (user_id, location_id);


--
-- Name: user_role_assignments uq_user_role_assignment; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_role_assignments
    ADD CONSTRAINT uq_user_role_assignment UNIQUE (company_id, user_id, role_id);


--
-- Name: user_tour_progress uq_utp_user_tour; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tour_progress
    ADD CONSTRAINT uq_utp_user_tour UNIQUE (user_id, tour_id);


--
-- Name: user_locations user_locations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_locations
    ADD CONSTRAINT user_locations_pkey PRIMARY KEY (id);


--
-- Name: user_role_assignments user_role_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_role_assignments
    ADD CONSTRAINT user_role_assignments_pkey PRIMARY KEY (id);


--
-- Name: user_tour_progress user_tour_progress_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tour_progress
    ADD CONSTRAINT user_tour_progress_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: vehicle_inspections vehicle_inspections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vehicle_inspections
    ADD CONSTRAINT vehicle_inspections_pkey PRIMARY KEY (id);


--
-- Name: vehicle_service_records vehicle_service_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vehicle_service_records
    ADD CONSTRAINT vehicle_service_records_pkey PRIMARY KEY (id);


--
-- Name: vehicles vehicles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vehicles
    ADD CONSTRAINT vehicles_pkey PRIMARY KEY (id);


--
-- Name: vehicles vehicles_vin_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vehicles
    ADD CONSTRAINT vehicles_vin_key UNIQUE (vin);


--
-- Name: vendor_bill_payments vendor_bill_payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_bill_payments
    ADD CONSTRAINT vendor_bill_payments_pkey PRIMARY KEY (id);


--
-- Name: vendor_invoice_lines vendor_invoice_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_invoice_lines
    ADD CONSTRAINT vendor_invoice_lines_pkey PRIMARY KEY (id);


--
-- Name: vendor_invoices vendor_invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_invoices
    ADD CONSTRAINT vendor_invoices_pkey PRIMARY KEY (id);


--
-- Name: vendor_order_lines vendor_order_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_order_lines
    ADD CONSTRAINT vendor_order_lines_pkey PRIMARY KEY (id);


--
-- Name: vendor_orders vendor_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_orders
    ADD CONSTRAINT vendor_orders_pkey PRIMARY KEY (id);


--
-- Name: vendor_statement_lines vendor_statement_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_statement_lines
    ADD CONSTRAINT vendor_statement_lines_pkey PRIMARY KEY (id);


--
-- Name: vendor_statements vendor_statements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_statements
    ADD CONSTRAINT vendor_statements_pkey PRIMARY KEY (id);


--
-- Name: vendors vendors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendors
    ADD CONSTRAINT vendors_pkey PRIMARY KEY (id);


--
-- Name: warranties warranties_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warranties
    ADD CONSTRAINT warranties_pkey PRIMARY KEY (id);


--
-- Name: warranty_claims warranty_claims_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warranty_claims
    ADD CONSTRAINT warranty_claims_pkey PRIMARY KEY (id);


--
-- Name: webhook_deliveries webhook_deliveries_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: webhook_deliveries webhook_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_pkey PRIMARY KEY (id);


--
-- Name: webhook_delivery_logs webhook_delivery_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_delivery_logs
    ADD CONSTRAINT webhook_delivery_logs_pkey PRIMARY KEY (id);


--
-- Name: webhook_endpoints webhook_endpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_endpoints
    ADD CONSTRAINT webhook_endpoints_pkey PRIMARY KEY (id);


--
-- Name: webhook_subscriptions webhook_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_subscriptions
    ADD CONSTRAINT webhook_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: winback_campaigns winback_campaigns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.winback_campaigns
    ADD CONSTRAINT winback_campaigns_pkey PRIMARY KEY (id);


--
-- Name: winback_sends winback_sends_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.winback_sends
    ADD CONSTRAINT winback_sends_pkey PRIMARY KEY (id);


--
-- Name: workflow_rules workflow_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_rules
    ADD CONSTRAINT workflow_rules_pkey PRIMARY KEY (id);


--
-- Name: workflow_runs workflow_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_runs
    ADD CONSTRAINT workflow_runs_pkey PRIMARY KEY (id);


--
-- Name: ix_api_keys_key_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_api_keys_key_hash ON public.api_keys USING btree (key_hash);


--
-- Name: ix_api_keys_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_tenant_id ON public.api_keys USING btree (tenant_id);


--
-- Name: ix_appointments_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_appointments_company_id ON public.appointments USING btree (company_id);


--
-- Name: ix_appointments_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_appointments_customer_id ON public.appointments USING btree (customer_id);


--
-- Name: ix_appointments_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_appointments_job_id ON public.appointments USING btree (job_id);


--
-- Name: ix_appointments_start_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_appointments_start_at ON public.appointments USING btree (start_at);


--
-- Name: ix_appointments_tech_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_appointments_tech_id ON public.appointments USING btree (tech_id);


--
-- Name: ix_audit_logs_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_action ON public.audit_logs USING btree (action);


--
-- Name: ix_audit_logs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_created_at ON public.audit_logs USING btree (created_at);


--
-- Name: ix_audit_logs_entity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_entity_id ON public.audit_logs USING btree (entity_id);


--
-- Name: ix_audit_logs_entity_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_entity_type ON public.audit_logs USING btree (entity_type);


--
-- Name: ix_audit_logs_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_request_id ON public.audit_logs USING btree (request_id);


--
-- Name: ix_audit_logs_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_tenant_id ON public.audit_logs USING btree (tenant_id);


--
-- Name: ix_audit_logs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_user_id ON public.audit_logs USING btree (user_id);


--
-- Name: ix_bank_feed_accounts_bank_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_accounts_bank_account_id ON public.bank_feed_accounts USING btree (bank_account_id);


--
-- Name: ix_bank_feed_accounts_connection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_accounts_connection_id ON public.bank_feed_accounts USING btree (connection_id);


--
-- Name: ix_bank_feed_balance_snapshots_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_balance_snapshots_account_id ON public.bank_feed_balance_snapshots USING btree (account_id);


--
-- Name: ix_bank_feed_documents_connection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_documents_connection_id ON public.bank_feed_documents USING btree (connection_id);


--
-- Name: ix_bank_feed_documents_document_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_documents_document_date ON public.bank_feed_documents USING btree (document_date);


--
-- Name: ix_bank_feed_documents_sha256; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_documents_sha256 ON public.bank_feed_documents USING btree (sha256);


--
-- Name: ix_bank_feed_sync_schedule_next_run_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_sync_schedule_next_run_at ON public.bank_feed_sync_schedule USING btree (next_run_at);


--
-- Name: ix_bank_feed_transactions_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_transactions_account_id ON public.bank_feed_transactions USING btree (account_id);


--
-- Name: ix_bank_feed_transactions_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_transactions_deleted_at ON public.bank_feed_transactions USING btree (deleted_at);


--
-- Name: ix_bank_feed_transactions_external_last_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_transactions_external_last_updated ON public.bank_feed_transactions USING btree (external_last_updated);


--
-- Name: ix_bank_feed_transactions_line_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_transactions_line_hash ON public.bank_feed_transactions USING btree (line_hash);


--
-- Name: ix_bank_feed_transactions_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_transactions_pending ON public.bank_feed_transactions USING btree (pending);


--
-- Name: ix_bank_feed_txn_acct_posted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_feed_txn_acct_posted ON public.bank_feed_transactions USING btree (account_id, posted_date);


--
-- Name: ix_bank_match_externals_match_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_match_externals_match_id ON public.bank_match_externals USING btree (match_id);


--
-- Name: ix_bank_match_lines_line_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_match_lines_line_id ON public.bank_match_lines USING btree (line_id);


--
-- Name: ix_bank_match_lines_match_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_match_lines_match_id ON public.bank_match_lines USING btree (match_id);


--
-- Name: ix_bank_matches_bank_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_matches_bank_account_id ON public.bank_matches USING btree (bank_account_id);


--
-- Name: ix_bank_matches_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_matches_status ON public.bank_matches USING btree (status);


--
-- Name: ix_bank_statement_imports_bank_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_statement_imports_bank_account_id ON public.bank_statement_imports USING btree (bank_account_id);


--
-- Name: ix_bank_statement_line_images_import_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_statement_line_images_import_id ON public.bank_statement_line_images USING btree (import_id);


--
-- Name: ix_bank_statement_line_images_line_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_statement_line_images_line_id ON public.bank_statement_line_images USING btree (line_id);


--
-- Name: ix_bank_statement_line_sources_import_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_statement_line_sources_import_id ON public.bank_statement_line_sources USING btree (import_id);


--
-- Name: ix_bank_statement_line_sources_line_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_statement_line_sources_line_id ON public.bank_statement_line_sources USING btree (line_id);


--
-- Name: ix_bank_statement_lines_bank_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_statement_lines_bank_account_id ON public.bank_statement_lines USING btree (bank_account_id);


--
-- Name: ix_bank_statement_lines_import_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_statement_lines_import_id ON public.bank_statement_lines USING btree (import_id);


--
-- Name: ix_bank_stmt_lines_acct_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_stmt_lines_acct_date ON public.bank_statement_lines USING btree (bank_account_id, txn_date);


--
-- Name: ix_banno_connections_institution_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_banno_connections_institution_id ON public.banno_connections USING btree (institution_id);


--
-- Name: ix_catalog_items_sku; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_catalog_items_sku ON public.catalog_items USING btree (sku);


--
-- Name: ix_catalog_items_wholesaler_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_catalog_items_wholesaler_tenant_id ON public.catalog_items USING btree (wholesaler_tenant_id);


--
-- Name: ix_change_orders_billed_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_change_orders_billed_invoice_id ON public.change_orders USING btree (billed_invoice_id);


--
-- Name: ix_change_orders_co_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_change_orders_co_number ON public.change_orders USING btree (co_number);


--
-- Name: ix_change_orders_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_change_orders_job_id ON public.change_orders USING btree (job_id);


--
-- Name: ix_channel_analytics_wholesaler_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_channel_analytics_wholesaler_tenant_id ON public.channel_analytics USING btree (wholesaler_tenant_id);


--
-- Name: ix_chi_door_catalog_pricing_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chi_door_catalog_pricing_category ON public.chi_door_catalog USING btree (pricing_category);


--
-- Name: ix_chi_parts_catalog_pricing_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chi_parts_catalog_pricing_category ON public.chi_parts_catalog USING btree (pricing_category);


--
-- Name: ix_custom_catalog_items_catalog_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_catalog_items_catalog_id ON public.custom_catalog_items USING btree (catalog_id);


--
-- Name: ix_custom_catalog_items_pricing_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_catalog_items_pricing_category ON public.custom_catalog_items USING btree (pricing_category);


--
-- Name: ix_custom_catalog_items_product_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_catalog_items_product_class ON public.custom_catalog_items USING btree (product_class);


--
-- Name: ix_custom_catalog_items_qb_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_catalog_items_qb_item_id ON public.custom_catalog_items USING btree (qb_item_id);


--
-- Name: ix_custom_catalogs_product_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_catalogs_product_class ON public.custom_catalogs USING btree (product_class);


--
-- Name: ix_custom_field_definitions_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_field_definitions_company_id ON public.custom_field_definitions USING btree (company_id);


--
-- Name: ix_custom_field_definitions_entity_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_field_definitions_entity_type ON public.custom_field_definitions USING btree (entity_type);


--
-- Name: ix_custom_field_values_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_field_values_company_id ON public.custom_field_values USING btree (company_id);


--
-- Name: ix_custom_field_values_definition_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_field_values_definition_id ON public.custom_field_values USING btree (definition_id);


--
-- Name: ix_custom_field_values_entity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_field_values_entity_id ON public.custom_field_values USING btree (entity_id);


--
-- Name: ix_customer_contacts_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_customer_contacts_company_id ON public.customer_contacts USING btree (company_id);


--
-- Name: ix_customer_contacts_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_customer_contacts_customer_id ON public.customer_contacts USING btree (customer_id);


--
-- Name: ix_customer_volume_discount_tiers_settings_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_customer_volume_discount_tiers_settings_id ON public.customer_volume_discount_tiers USING btree (settings_id);


--
-- Name: ix_customers_email_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_customers_email_hash ON public.customers USING btree (email_hash);


--
-- Name: ix_customers_name_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_customers_name_hash ON public.customers USING btree (name_hash);


--
-- Name: ix_customers_phone_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_customers_phone_hash ON public.customers USING btree (phone_hash);


--
-- Name: ix_dealer_orders_dealer_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dealer_orders_dealer_tenant_id ON public.dealer_orders USING btree (dealer_tenant_id);


--
-- Name: ix_dealer_orders_distributor_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dealer_orders_distributor_tenant_id ON public.dealer_orders USING btree (distributor_tenant_id);


--
-- Name: ix_dealer_orders_idempotency_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dealer_orders_idempotency_key ON public.dealer_orders USING btree (idempotency_key);


--
-- Name: ix_dealer_orders_order_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dealer_orders_order_number ON public.dealer_orders USING btree (order_number);


--
-- Name: ix_device_alert_registrations_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_device_alert_registrations_user_id ON public.device_alert_registrations USING btree (user_id);


--
-- Name: ix_device_tokens_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_device_tokens_tenant_id ON public.device_tokens USING btree (tenant_id);


--
-- Name: ix_device_tokens_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_device_tokens_user_id ON public.device_tokens USING btree (user_id);


--
-- Name: ix_distributor_analytics_distributor_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_distributor_analytics_distributor_tenant_id ON public.distributor_analytics USING btree (distributor_tenant_id);


--
-- Name: ix_document_folders_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_folders_parent_id ON public.document_folders USING btree (parent_id);


--
-- Name: ix_document_signatures_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_signatures_company_id ON public.document_signatures USING btree (company_id);


--
-- Name: ix_document_signatures_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_signatures_document_id ON public.document_signatures USING btree (document_id);


--
-- Name: ix_document_signatures_document_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_signatures_document_type ON public.document_signatures USING btree (document_type);


--
-- Name: ix_document_signatures_token; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_document_signatures_token ON public.document_signatures USING btree (token);


--
-- Name: ix_documents_content_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_documents_content_hash ON public.documents USING btree (content_hash);


--
-- Name: ix_door_listing_photos_listing_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_door_listing_photos_listing_id ON public.door_listing_photos USING btree (listing_id);


--
-- Name: ix_door_listings_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_door_listings_slug ON public.door_listings USING btree (slug);


--
-- Name: ix_door_listings_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_door_listings_source ON public.door_listings USING btree (source);


--
-- Name: ix_door_listings_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_door_listings_status ON public.door_listings USING btree (status);


--
-- Name: ix_door_listings_submitted_by_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_door_listings_submitted_by_customer_id ON public.door_listings USING btree (submitted_by_customer_id);


--
-- Name: ix_email_vendor_bill_sweep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_email_vendor_bill_sweep ON public.outlook_messages USING btree (account_id, received_at) WHERE (has_attachments AND (vendor_bills_ingested_at IS NULL));


--
-- Name: ix_estimate_lines_labor_price_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_estimate_lines_labor_price_item_id ON public.estimate_lines USING btree (labor_price_item_id);


--
-- Name: ix_estimates_lead_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_estimates_lead_id ON public.estimates USING btree (lead_id);


--
-- Name: ix_expense_receipts_expense_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_expense_receipts_expense_id ON public.expense_receipts USING btree (expense_id);


--
-- Name: ix_expense_receipts_sha256; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_expense_receipts_sha256 ON public.expense_receipts USING btree (sha256);


--
-- Name: ix_follow_ups_assigned_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_follow_ups_assigned_to ON public.follow_ups USING btree (assigned_to);


--
-- Name: ix_follow_ups_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_follow_ups_company_id ON public.follow_ups USING btree (company_id);


--
-- Name: ix_follow_ups_due_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_follow_ups_due_date ON public.follow_ups USING btree (due_date);


--
-- Name: ix_follow_ups_entity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_follow_ups_entity_id ON public.follow_ups USING btree (entity_id);


--
-- Name: ix_forecast_snapshot_invoices_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_forecast_snapshot_invoices_invoice_id ON public.forecast_snapshot_invoices USING btree (invoice_id);


--
-- Name: ix_forecast_snapshot_invoices_snap_bucket; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_forecast_snapshot_invoices_snap_bucket ON public.forecast_snapshot_invoices USING btree (snapshot_id, bucket);


--
-- Name: ix_forecast_snapshot_invoices_snapshot_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_forecast_snapshot_invoices_snapshot_id ON public.forecast_snapshot_invoices USING btree (snapshot_id);


--
-- Name: ix_forecast_snapshots_as_of; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_forecast_snapshots_as_of ON public.forecast_snapshots USING btree (as_of);


--
-- Name: ix_forecast_snapshots_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_forecast_snapshots_status ON public.forecast_snapshots USING btree (status);


--
-- Name: ix_forecast_snapshots_status_horizon; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_forecast_snapshots_status_horizon ON public.forecast_snapshots USING btree (status, horizon_end);


--
-- Name: ix_gl_accounts_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gl_accounts_code ON public.gl_accounts USING btree (code);


--
-- Name: ix_gl_accounts_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gl_accounts_role ON public.gl_accounts USING btree (role);


--
-- Name: ix_gl_journal_entries_effective_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gl_journal_entries_effective_at ON public.gl_journal_entries USING btree (effective_at);


--
-- Name: ix_gl_journal_entries_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gl_journal_entries_source ON public.gl_journal_entries USING btree (source_type, source_id);


--
-- Name: ix_gl_journal_lines_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gl_journal_lines_account_id ON public.gl_journal_lines USING btree (account_id);


--
-- Name: ix_gl_journal_lines_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gl_journal_lines_customer_id ON public.gl_journal_lines USING btree (customer_id);


--
-- Name: ix_gl_journal_lines_entry_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gl_journal_lines_entry_id ON public.gl_journal_lines USING btree (entry_id);


--
-- Name: ix_gl_journal_lines_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gl_journal_lines_job_id ON public.gl_journal_lines USING btree (job_id);


--
-- Name: ix_hubx_door_orders_graph_message_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hubx_door_orders_graph_message_id ON public.hubx_door_orders USING btree (graph_message_id);


--
-- Name: ix_hubx_door_orders_matched_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hubx_door_orders_matched_job_id ON public.hubx_door_orders USING btree (matched_job_id);


--
-- Name: ix_hubx_door_orders_qcd; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_hubx_door_orders_qcd ON public.hubx_door_orders USING btree (qcd);


--
-- Name: ix_inbound_emails_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inbound_emails_company_id ON public.inbound_emails USING btree (company_id);


--
-- Name: ix_inbound_emails_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inbound_emails_customer_id ON public.inbound_emails USING btree (customer_id);


--
-- Name: ix_inbound_emails_from_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inbound_emails_from_email ON public.inbound_emails USING btree (from_email);


--
-- Name: ix_inbound_emails_provider_message_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inbound_emails_provider_message_id ON public.inbound_emails USING btree (provider_message_id);


--
-- Name: ix_integration_configs_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_integration_configs_tenant_id ON public.integration_configs USING btree (tenant_id);


--
-- Name: ix_internal_tasks_assigned_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_internal_tasks_assigned_to ON public.internal_tasks USING btree (assigned_to);


--
-- Name: ix_internal_tasks_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_internal_tasks_company_id ON public.internal_tasks USING btree (company_id);


--
-- Name: ix_internal_tasks_related_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_internal_tasks_related_customer_id ON public.internal_tasks USING btree (related_customer_id);


--
-- Name: ix_internal_tasks_related_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_internal_tasks_related_job_id ON public.internal_tasks USING btree (related_job_id);


--
-- Name: ix_internal_tasks_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_internal_tasks_status ON public.internal_tasks USING btree (status);


--
-- Name: ix_inventory_items_sku; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inventory_items_sku ON public.inventory_items USING btree (sku);


--
-- Name: ix_invoice_adjustments_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoice_adjustments_invoice_id ON public.invoice_adjustments USING btree (invoice_id);


--
-- Name: ix_invoice_lines_labor_price_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoice_lines_labor_price_item_id ON public.invoice_lines USING btree (labor_price_item_id);


--
-- Name: ix_invoice_lines_part_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoice_lines_part_id ON public.invoice_lines USING btree (part_id);


--
-- Name: ix_invoices_estimate_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoices_estimate_id ON public.invoices USING btree (estimate_id);


--
-- Name: ix_invoices_source_estimate_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoices_source_estimate_id ON public.invoices USING btree (source_estimate_id);


--
-- Name: ix_job_assignments_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_assignments_job_id ON public.job_assignments USING btree (job_id);


--
-- Name: ix_job_assignments_tech_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_assignments_tech_id ON public.job_assignments USING btree (tech_id);


--
-- Name: ix_job_chat_messages_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_chat_messages_company_id ON public.job_chat_messages USING btree (company_id);


--
-- Name: ix_job_chat_messages_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_chat_messages_created_at ON public.job_chat_messages USING btree (created_at);


--
-- Name: ix_job_chat_messages_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_chat_messages_job_id ON public.job_chat_messages USING btree (job_id);


--
-- Name: ix_job_closeouts_billed_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_closeouts_billed_invoice_id ON public.job_closeouts USING btree (billed_invoice_id);


--
-- Name: ix_job_closeouts_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_closeouts_job_id ON public.job_closeouts USING btree (job_id);


--
-- Name: ix_job_diagnoses_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_diagnoses_job_id ON public.job_diagnoses USING btree (job_id);


--
-- Name: ix_job_diagnoses_service_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_diagnoses_service_type ON public.job_diagnoses USING btree (service_type);


--
-- Name: ix_job_hazards_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_hazards_customer_id ON public.job_hazards USING btree (customer_id);


--
-- Name: ix_job_hazards_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_hazards_job_id ON public.job_hazards USING btree (job_id);


--
-- Name: ix_job_notes_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_notes_company_id ON public.job_notes USING btree (company_id);


--
-- Name: ix_job_notes_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_notes_job_id ON public.job_notes USING btree (job_id);


--
-- Name: ix_job_parts_needed_billed_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_parts_needed_billed_invoice_id ON public.job_parts_needed USING btree (billed_invoice_id);


--
-- Name: ix_job_photos_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_photos_company_id ON public.job_photos USING btree (company_id);


--
-- Name: ix_job_photos_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_photos_job_id ON public.job_photos USING btree (job_id);


--
-- Name: ix_job_receipts_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_receipts_job_id ON public.job_receipts USING btree (job_id);


--
-- Name: ix_jobs_job_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_job_number ON public.jobs USING btree (job_number);


--
-- Name: ix_jobs_location_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_location_id ON public.jobs USING btree (location_id);


--
-- Name: ix_labor_price_items_service_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_labor_price_items_service_type ON public.labor_price_items USING btree (service_type);


--
-- Name: ix_labor_price_items_sku; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_labor_price_items_sku ON public.labor_price_items USING btree (sku);


--
-- Name: ix_landing_leads_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_landing_leads_company_id ON public.landing_leads USING btree (company_id);


--
-- Name: ix_landing_leads_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_landing_leads_source ON public.landing_leads USING btree (source);


--
-- Name: ix_landing_leads_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_landing_leads_status ON public.landing_leads USING btree (status);


--
-- Name: ix_leads_assigned_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leads_assigned_to ON public.leads USING btree (assigned_to);


--
-- Name: ix_leads_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leads_company_id ON public.leads USING btree (company_id);


--
-- Name: ix_leads_estimate_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leads_estimate_id ON public.leads USING btree (estimate_id);


--
-- Name: ix_leads_follow_up_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leads_follow_up_date ON public.leads USING btree (follow_up_date);


--
-- Name: ix_leads_landing_lead_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leads_landing_lead_id ON public.leads USING btree (landing_lead_id);


--
-- Name: ix_leads_stage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leads_stage ON public.leads USING btree (stage);


--
-- Name: ix_loyalty_points_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_loyalty_points_customer_id ON public.loyalty_points USING btree (customer_id);


--
-- Name: ix_maintenance_plans_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_maintenance_plans_company_id ON public.maintenance_plans USING btree (company_id);


--
-- Name: ix_margin_tiers_tier_set_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_margin_tiers_tier_set_id ON public.margin_tiers USING btree (tier_set_id);


--
-- Name: ix_markup_rules_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_markup_rules_company_id ON public.markup_rules USING btree (company_id);


--
-- Name: ix_monthly_budgets_year_month; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_monthly_budgets_year_month ON public.monthly_budgets USING btree (year, month);


--
-- Name: ix_next_actions_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_next_actions_tenant_id ON public.next_actions USING btree (tenant_id);


--
-- Name: ix_next_actions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_next_actions_user_id ON public.next_actions USING btree (user_id);


--
-- Name: ix_notification_log_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_log_tenant_id ON public.notification_log USING btree (tenant_id);


--
-- Name: ix_notification_log_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_log_user_id ON public.notification_log USING btree (user_id);


--
-- Name: ix_notification_preferences_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_preferences_tenant_id ON public.notification_preferences USING btree (tenant_id);


--
-- Name: ix_notification_preferences_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_preferences_user_id ON public.notification_preferences USING btree (user_id);


--
-- Name: ix_onboarding_state_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_onboarding_state_company_id ON public.onboarding_state USING btree (company_id);


--
-- Name: ix_outbound_emails_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outbound_emails_company_id ON public.outbound_emails USING btree (company_id);


--
-- Name: ix_outbound_emails_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outbound_emails_created_at ON public.outbound_emails USING btree (created_at);


--
-- Name: ix_outbound_emails_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outbound_emails_entity ON public.outbound_emails USING btree (entity_type, entity_id);


--
-- Name: ix_outlook_accounts_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_accounts_user_id ON public.outlook_accounts USING btree (user_id);


--
-- Name: ix_outlook_attachments_message_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_attachments_message_id ON public.outlook_attachments USING btree (message_id);


--
-- Name: ix_outlook_folder_prefs_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_folder_prefs_account_id ON public.outlook_folder_prefs USING btree (account_id);


--
-- Name: ix_outlook_folder_prefs_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_folder_prefs_folder_id ON public.outlook_folder_prefs USING btree (folder_id);


--
-- Name: ix_outlook_folder_sync_state_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_folder_sync_state_account_id ON public.outlook_folder_sync_state USING btree (account_id);


--
-- Name: ix_outlook_folder_sync_state_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_folder_sync_state_folder_id ON public.outlook_folder_sync_state USING btree (folder_id);


--
-- Name: ix_outlook_folders_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_folders_account_id ON public.outlook_folders USING btree (account_id);


--
-- Name: ix_outlook_folders_graph_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_folders_graph_folder_id ON public.outlook_folders USING btree (graph_folder_id);


--
-- Name: ix_outlook_folders_parent_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_folders_parent_folder_id ON public.outlook_folders USING btree (parent_folder_id);


--
-- Name: ix_outlook_folders_well_known_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_folders_well_known_name ON public.outlook_folders USING btree (well_known_name);


--
-- Name: ix_outlook_messages_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_messages_account_id ON public.outlook_messages USING btree (account_id);


--
-- Name: ix_outlook_messages_conversation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_messages_conversation_id ON public.outlook_messages USING btree (conversation_id);


--
-- Name: ix_outlook_messages_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_messages_folder_id ON public.outlook_messages USING btree (folder_id);


--
-- Name: ix_outlook_messages_from_address; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_messages_from_address ON public.outlook_messages USING btree (from_address);


--
-- Name: ix_outlook_messages_graph_message_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_messages_graph_message_id ON public.outlook_messages USING btree (graph_message_id);


--
-- Name: ix_outlook_messages_internet_message_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_messages_internet_message_id ON public.outlook_messages USING btree (internet_message_id);


--
-- Name: ix_outlook_messages_linked_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_messages_linked_customer_id ON public.outlook_messages USING btree (linked_customer_id);


--
-- Name: ix_outlook_messages_linked_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_messages_linked_job_id ON public.outlook_messages USING btree (linked_job_id);


--
-- Name: ix_outlook_messages_received_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_messages_received_at ON public.outlook_messages USING btree (received_at);


--
-- Name: ix_outlook_messages_sent_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outlook_messages_sent_at ON public.outlook_messages USING btree (sent_at);


--
-- Name: ix_overhead_obligations_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_overhead_obligations_active ON public.overhead_obligations USING btree (company_id, active, deleted_at);


--
-- Name: ix_overhead_obligations_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_overhead_obligations_company ON public.overhead_obligations USING btree (company_id);


--
-- Name: ix_overhead_obligations_source_stream_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_overhead_obligations_source_stream_id ON public.overhead_obligations USING btree (source_stream_id);


--
-- Name: ix_part_prices_part_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_part_prices_part_number ON public.part_prices USING btree (part_number);


--
-- Name: ix_part_prices_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_part_prices_tenant_id ON public.part_prices USING btree (tenant_id);


--
-- Name: ix_payment_plan_installments_plan_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payment_plan_installments_plan_id ON public.payment_plan_installments USING btree (plan_id);


--
-- Name: ix_payment_plans_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payment_plans_invoice_id ON public.payment_plans USING btree (invoice_id);


--
-- Name: ix_payment_reminders_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payment_reminders_invoice_id ON public.payment_reminders USING btree (invoice_id);


--
-- Name: ix_payroll_entries_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_entries_company_id ON public.payroll_entries USING btree (company_id);


--
-- Name: ix_payroll_entries_period_end; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_entries_period_end ON public.payroll_entries USING btree (period_end);


--
-- Name: ix_payroll_entries_period_start; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_entries_period_start ON public.payroll_entries USING btree (period_start);


--
-- Name: ix_payroll_entries_tech_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_entries_tech_user_id ON public.payroll_entries USING btree (tech_user_id);


--
-- Name: ix_phone_com_calls_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_calls_customer_id ON public.phone_com_calls USING btree (customer_id);


--
-- Name: ix_phone_com_calls_extension_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_calls_extension_id ON public.phone_com_calls USING btree (extension_id);


--
-- Name: ix_phone_com_calls_from_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_calls_from_number ON public.phone_com_calls USING btree (from_number);


--
-- Name: ix_phone_com_calls_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_calls_job_id ON public.phone_com_calls USING btree (job_id);


--
-- Name: ix_phone_com_calls_phone_com_call_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_phone_com_calls_phone_com_call_id ON public.phone_com_calls USING btree (phone_com_call_id);


--
-- Name: ix_phone_com_calls_started_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_calls_started_at ON public.phone_com_calls USING btree (started_at);


--
-- Name: ix_phone_com_calls_to_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_calls_to_number ON public.phone_com_calls USING btree (to_number);


--
-- Name: ix_phone_com_contact_push_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_contact_push_customer_id ON public.phone_com_contact_push USING btree (customer_id);


--
-- Name: ix_phone_com_contact_push_phone_com_contact_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_contact_push_phone_com_contact_id ON public.phone_com_contact_push USING btree (phone_com_contact_id);


--
-- Name: ix_phone_com_contact_push_phone_e164; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_contact_push_phone_e164 ON public.phone_com_contact_push USING btree (phone_e164);


--
-- Name: ix_phone_com_extensions_phone_com_extension_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_phone_com_extensions_phone_com_extension_id ON public.phone_com_extensions USING btree (phone_com_extension_id);


--
-- Name: ix_phone_com_faxes_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_faxes_customer_id ON public.phone_com_faxes USING btree (customer_id);


--
-- Name: ix_phone_com_faxes_from_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_faxes_from_number ON public.phone_com_faxes USING btree (from_number);


--
-- Name: ix_phone_com_faxes_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_faxes_job_id ON public.phone_com_faxes USING btree (job_id);


--
-- Name: ix_phone_com_faxes_phone_com_fax_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_phone_com_faxes_phone_com_fax_id ON public.phone_com_faxes USING btree (phone_com_fax_id);


--
-- Name: ix_phone_com_faxes_received_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_faxes_received_at ON public.phone_com_faxes USING btree (received_at);


--
-- Name: ix_phone_com_faxes_to_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_faxes_to_number ON public.phone_com_faxes USING btree (to_number);


--
-- Name: ix_phone_com_messages_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_messages_customer_id ON public.phone_com_messages USING btree (customer_id);


--
-- Name: ix_phone_com_messages_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_messages_job_id ON public.phone_com_messages USING btree (job_id);


--
-- Name: ix_phone_com_messages_phone_com_conversation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_messages_phone_com_conversation_id ON public.phone_com_messages USING btree (phone_com_conversation_id);


--
-- Name: ix_phone_com_messages_phone_com_message_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_phone_com_messages_phone_com_message_id ON public.phone_com_messages USING btree (phone_com_message_id);


--
-- Name: ix_phone_com_messages_sent_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_messages_sent_at ON public.phone_com_messages USING btree (sent_at);


--
-- Name: ix_phone_com_messages_thread_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_messages_thread_key ON public.phone_com_messages USING btree (thread_key);


--
-- Name: ix_phone_com_numbers_phone_com_number; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_phone_com_numbers_phone_com_number ON public.phone_com_numbers USING btree (phone_com_number);


--
-- Name: ix_phone_com_stats_daily_stat_date; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_phone_com_stats_daily_stat_date ON public.phone_com_stats_daily USING btree (stat_date);


--
-- Name: ix_phone_com_voicemails_call_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_voicemails_call_id ON public.phone_com_voicemails USING btree (call_id);


--
-- Name: ix_phone_com_voicemails_extension_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_com_voicemails_extension_id ON public.phone_com_voicemails USING btree (extension_id);


--
-- Name: ix_phone_com_voicemails_phone_com_voicemail_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_phone_com_voicemails_phone_com_voicemail_id ON public.phone_com_voicemails USING btree (phone_com_voicemail_id);


--
-- Name: ix_plan_enrollments_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plan_enrollments_company_id ON public.plan_enrollments USING btree (company_id);


--
-- Name: ix_plan_enrollments_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plan_enrollments_customer_id ON public.plan_enrollments USING btree (customer_id);


--
-- Name: ix_plan_enrollments_next_service_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plan_enrollments_next_service_date ON public.plan_enrollments USING btree (next_service_date);


--
-- Name: ix_plan_enrollments_plan_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plan_enrollments_plan_id ON public.plan_enrollments USING btree (plan_id);


--
-- Name: ix_plugin_email_outbox_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plugin_email_outbox_company_id ON public.plugin_email_outbox USING btree (company_id);


--
-- Name: ix_plugin_email_outbox_plugin_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plugin_email_outbox_plugin_key ON public.plugin_email_outbox USING btree (plugin_key);


--
-- Name: ix_plugin_email_outbox_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plugin_email_outbox_status ON public.plugin_email_outbox USING btree (status);


--
-- Name: ix_pricing_tier_sets_pricing_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pricing_tier_sets_pricing_category ON public.pricing_tier_sets USING btree (pricing_category);


--
-- Name: ix_pricing_tier_sets_pricing_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pricing_tier_sets_pricing_class ON public.pricing_tier_sets USING btree (pricing_class);


--
-- Name: ix_pricing_tiers_distributor_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pricing_tiers_distributor_tenant_id ON public.pricing_tiers USING btree (distributor_tenant_id);


--
-- Name: ix_pricing_tiers_wholesaler_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pricing_tiers_wholesaler_tenant_id ON public.pricing_tiers USING btree (wholesaler_tenant_id);


--
-- Name: ix_proposal_tier_lines_tier_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proposal_tier_lines_tier_id ON public.proposal_tier_lines USING btree (tier_id);


--
-- Name: ix_purchase_order_lines_po_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_purchase_order_lines_po_id ON public.purchase_order_lines USING btree (po_id);


--
-- Name: ix_purchase_orders_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_purchase_orders_job_id ON public.purchase_orders USING btree (job_id);


--
-- Name: ix_purchase_orders_po_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_purchase_orders_po_number ON public.purchase_orders USING btree (po_number);


--
-- Name: ix_purchase_orders_vendor_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_purchase_orders_vendor_id ON public.purchase_orders USING btree (vendor_id);


--
-- Name: ix_push_subscriptions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_push_subscriptions_user_id ON public.push_subscriptions USING btree (user_id);


--
-- Name: ix_qb_bank_transactions_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_bank_transactions_deleted_at ON public.qb_bank_transactions USING btree (deleted_at);


--
-- Name: ix_qb_bank_transactions_qb_txn_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_qb_bank_transactions_qb_txn_id ON public.qb_bank_transactions USING btree (qb_txn_id);


--
-- Name: ix_qb_bank_transactions_txn_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_bank_transactions_txn_date ON public.qb_bank_transactions USING btree (txn_date);


--
-- Name: ix_qb_banking_entries_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_banking_entries_account_id ON public.qb_banking_entries USING btree (account_id);


--
-- Name: ix_qb_banking_entries_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_banking_entries_deleted_at ON public.qb_banking_entries USING btree (deleted_at);


--
-- Name: ix_qb_banking_entries_qb_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_banking_entries_qb_entity ON public.qb_banking_entries USING btree (qb_entity);


--
-- Name: ix_qb_banking_entries_qb_txn_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_banking_entries_qb_txn_id ON public.qb_banking_entries USING btree (qb_txn_id);


--
-- Name: ix_qb_banking_entries_txn_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_banking_entries_txn_date ON public.qb_banking_entries USING btree (txn_date);


--
-- Name: ix_qb_connections_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_qb_connections_tenant_id ON public.qb_connections USING btree (tenant_id);


--
-- Name: ix_qb_deposits_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_deposits_deleted_at ON public.qb_deposits USING btree (deleted_at);


--
-- Name: ix_qb_deposits_deposit_to_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_deposits_deposit_to_account_id ON public.qb_deposits USING btree (deposit_to_account_id);


--
-- Name: ix_qb_deposits_qb_txn_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_qb_deposits_qb_txn_id ON public.qb_deposits USING btree (qb_txn_id);


--
-- Name: ix_qb_deposits_txn_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_deposits_txn_date ON public.qb_deposits USING btree (txn_date);


--
-- Name: ix_qb_entity_maps_entity_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_entity_maps_entity_type ON public.qb_entity_maps USING btree (entity_type);


--
-- Name: ix_qb_entity_maps_local_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_entity_maps_local_id ON public.qb_entity_maps USING btree (local_id);


--
-- Name: ix_qb_entity_maps_qb_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_entity_maps_qb_id ON public.qb_entity_maps USING btree (qb_id);


--
-- Name: ix_qb_entity_maps_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_entity_maps_tenant_id ON public.qb_entity_maps USING btree (tenant_id);


--
-- Name: ix_qb_pnl_monthly_year_month; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_pnl_monthly_year_month ON public.qb_pnl_monthly USING btree (year, month);


--
-- Name: ix_qb_recurring_transactions_customer_qb_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_recurring_transactions_customer_qb_id ON public.qb_recurring_transactions USING btree (customer_qb_id);


--
-- Name: ix_qb_recurring_transactions_next_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_recurring_transactions_next_date ON public.qb_recurring_transactions USING btree (next_date);


--
-- Name: ix_qb_recurring_transactions_qb_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_qb_recurring_transactions_qb_id ON public.qb_recurring_transactions USING btree (qb_id);


--
-- Name: ix_qb_sync_schedule_next_run_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_sync_schedule_next_run_at ON public.qb_sync_schedule USING btree (next_run_at);


--
-- Name: ix_qb_token_store_realm_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_token_store_realm_id ON public.qb_token_store USING btree (realm_id);


--
-- Name: ix_qb_token_store_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_token_store_tenant_id ON public.qb_token_store USING btree (tenant_id);


--
-- Name: ix_qb_transfers_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_transfers_deleted_at ON public.qb_transfers USING btree (deleted_at);


--
-- Name: ix_qb_transfers_from_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_transfers_from_account_id ON public.qb_transfers USING btree (from_account_id);


--
-- Name: ix_qb_transfers_qb_txn_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_qb_transfers_qb_txn_id ON public.qb_transfers USING btree (qb_txn_id);


--
-- Name: ix_qb_transfers_to_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_transfers_to_account_id ON public.qb_transfers USING btree (to_account_id);


--
-- Name: ix_qb_transfers_txn_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_transfers_txn_date ON public.qb_transfers USING btree (txn_date);


--
-- Name: ix_qb_vendors_qb_vendor_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_vendors_qb_vendor_id ON public.qb_vendors USING btree (qb_vendor_id);


--
-- Name: ix_qb_vendors_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_vendors_tenant_id ON public.qb_vendors USING btree (tenant_id);


--
-- Name: ix_quote_request_photos_quote_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_quote_request_photos_quote_request_id ON public.quote_request_photos USING btree (quote_request_id);


--
-- Name: ix_quote_requests_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_quote_requests_customer_id ON public.quote_requests USING btree (customer_id);


--
-- Name: ix_quote_requests_lead_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_quote_requests_lead_id ON public.quote_requests USING btree (lead_id);


--
-- Name: ix_quote_templates_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_quote_templates_tenant_id ON public.quote_templates USING btree (tenant_id);


--
-- Name: ix_recurring_stream_hits_qb_txn_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recurring_stream_hits_qb_txn_id ON public.recurring_stream_hits USING btree (qb_txn_id);


--
-- Name: ix_recurring_stream_hits_stream_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recurring_stream_hits_stream_date ON public.recurring_stream_hits USING btree (stream_id, txn_date);


--
-- Name: ix_recurring_stream_hits_stream_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recurring_stream_hits_stream_id ON public.recurring_stream_hits USING btree (stream_id);


--
-- Name: ix_recurring_stream_hits_txn_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recurring_stream_hits_txn_date ON public.recurring_stream_hits USING btree (txn_date);


--
-- Name: ix_recurring_streams_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recurring_streams_deleted_at ON public.recurring_streams USING btree (deleted_at);


--
-- Name: ix_recurring_streams_next_expected_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recurring_streams_next_expected_date ON public.recurring_streams USING btree (next_expected_date);


--
-- Name: ix_recurring_streams_payee_pattern; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recurring_streams_payee_pattern ON public.recurring_streams USING btree (payee_pattern);


--
-- Name: ix_recurring_streams_source_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recurring_streams_source_status ON public.recurring_streams USING btree (source, status);


--
-- Name: ix_recurring_streams_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recurring_streams_status ON public.recurring_streams USING btree (status);


--
-- Name: ix_recurring_streams_status_next; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recurring_streams_status_next ON public.recurring_streams USING btree (status, next_expected_date);


--
-- Name: ix_reminder_settings_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_reminder_settings_company_id ON public.reminder_settings USING btree (company_id);


--
-- Name: ix_resale_quotes_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_resale_quotes_customer_id ON public.resale_quotes USING btree (customer_id);


--
-- Name: ix_resale_quotes_estimate_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_resale_quotes_estimate_id ON public.resale_quotes USING btree (estimate_id);


--
-- Name: ix_scheduled_sms_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduled_sms_customer_id ON public.scheduled_sms USING btree (customer_id);


--
-- Name: ix_scheduled_sms_entity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduled_sms_entity_id ON public.scheduled_sms USING btree (entity_id);


--
-- Name: ix_scheduled_sms_send_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduled_sms_send_at ON public.scheduled_sms USING btree (send_at);


--
-- Name: ix_scheduled_sms_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduled_sms_status ON public.scheduled_sms USING btree (status);


--
-- Name: ix_scheduled_sms_to_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduled_sms_to_number ON public.scheduled_sms USING btree (to_number);


--
-- Name: ix_service_agreement_templates_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_service_agreement_templates_company_id ON public.service_agreement_templates USING btree (company_id);


--
-- Name: ix_service_agreements_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_service_agreements_company_id ON public.service_agreements USING btree (company_id);


--
-- Name: ix_service_agreements_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_service_agreements_customer_id ON public.service_agreements USING btree (customer_id);


--
-- Name: ix_service_locations_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_service_locations_tenant_id ON public.service_locations USING btree (tenant_id);


--
-- Name: ix_stock_adjustments_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_stock_adjustments_item_id ON public.stock_adjustments USING btree (item_id);


--
-- Name: ix_support_tickets_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_support_tickets_tenant_id ON public.support_tickets USING btree (tenant_id);


--
-- Name: ix_survey_responses_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_survey_responses_company_id ON public.survey_responses USING btree (company_id);


--
-- Name: ix_survey_responses_send_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_survey_responses_send_id ON public.survey_responses USING btree (send_id);


--
-- Name: ix_survey_responses_template_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_survey_responses_template_id ON public.survey_responses USING btree (template_id);


--
-- Name: ix_survey_sends_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_survey_sends_company_id ON public.survey_sends USING btree (company_id);


--
-- Name: ix_survey_sends_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_survey_sends_customer_id ON public.survey_sends USING btree (customer_id);


--
-- Name: ix_survey_sends_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_survey_sends_job_id ON public.survey_sends USING btree (job_id);


--
-- Name: ix_survey_sends_template_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_survey_sends_template_id ON public.survey_sends USING btree (template_id);


--
-- Name: ix_survey_sends_token; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_survey_sends_token ON public.survey_sends USING btree (token);


--
-- Name: ix_survey_templates_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_survey_templates_company_id ON public.survey_templates USING btree (company_id);


--
-- Name: ix_tag_assignments_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tag_assignments_company_id ON public.tag_assignments USING btree (company_id);


--
-- Name: ix_tag_assignments_entity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tag_assignments_entity_id ON public.tag_assignments USING btree (entity_id);


--
-- Name: ix_tag_assignments_tag_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tag_assignments_tag_id ON public.tag_assignments USING btree (tag_id);


--
-- Name: ix_tags_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tags_company_id ON public.tags USING btree (company_id);


--
-- Name: ix_tax_exemption_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tax_exemption_customer_id ON public.tax_exemption USING btree (customer_id);


--
-- Name: ix_team_message_recipients_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_team_message_recipients_company_id ON public.team_message_recipients USING btree (company_id);


--
-- Name: ix_team_message_recipients_message_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_team_message_recipients_message_id ON public.team_message_recipients USING btree (message_id);


--
-- Name: ix_team_message_recipients_recipient_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_team_message_recipients_recipient_id ON public.team_message_recipients USING btree (recipient_id);


--
-- Name: ix_team_messages_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_team_messages_company_id ON public.team_messages USING btree (company_id);


--
-- Name: ix_team_messages_sender_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_team_messages_sender_id ON public.team_messages USING btree (sender_id);


--
-- Name: ix_tech_commission_rates_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tech_commission_rates_company_id ON public.tech_commission_rates USING btree (company_id);


--
-- Name: ix_tech_commission_rates_tech_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tech_commission_rates_tech_id ON public.tech_commission_rates USING btree (tech_id);


--
-- Name: ix_tech_locations_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tech_locations_job_id ON public.tech_locations USING btree (job_id);


--
-- Name: ix_tech_locations_recorded_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tech_locations_recorded_at ON public.tech_locations USING btree (recorded_at);


--
-- Name: ix_tech_locations_technician_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tech_locations_technician_id ON public.tech_locations USING btree (technician_id);


--
-- Name: ix_tech_locations_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tech_locations_user_id ON public.tech_locations USING btree (user_id);


--
-- Name: ix_technician_locations_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_technician_locations_company_id ON public.technician_locations USING btree (company_id);


--
-- Name: ix_technician_locations_recorded_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_technician_locations_recorded_at ON public.technician_locations USING btree (recorded_at);


--
-- Name: ix_technician_locations_tech_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_technician_locations_tech_id ON public.technician_locations USING btree (tech_id);


--
-- Name: ix_tenant_roles_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_roles_company_id ON public.tenant_roles USING btree (company_id);


--
-- Name: ix_time_entries_billed_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_time_entries_billed_invoice_id ON public.time_entries USING btree (billed_invoice_id);


--
-- Name: ix_time_off_requests_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_time_off_requests_status ON public.time_off_requests USING btree (status);


--
-- Name: ix_time_off_requests_technician_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_time_off_requests_technician_id ON public.time_off_requests USING btree (technician_id);


--
-- Name: ix_user_locations_location_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_locations_location_id ON public.user_locations USING btree (location_id);


--
-- Name: ix_user_locations_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_locations_user_id ON public.user_locations USING btree (user_id);


--
-- Name: ix_user_role_assignments_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_role_assignments_company_id ON public.user_role_assignments USING btree (company_id);


--
-- Name: ix_user_role_assignments_role_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_role_assignments_role_id ON public.user_role_assignments USING btree (role_id);


--
-- Name: ix_user_role_assignments_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_role_assignments_user_id ON public.user_role_assignments USING btree (user_id);


--
-- Name: ix_utp_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_utp_user_id ON public.user_tour_progress USING btree (user_id);


--
-- Name: ix_vehicle_inspections_inspection_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vehicle_inspections_inspection_at ON public.vehicle_inspections USING btree (inspection_at);


--
-- Name: ix_vehicle_inspections_technician_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vehicle_inspections_technician_id ON public.vehicle_inspections USING btree (technician_id);


--
-- Name: ix_vehicle_inspections_vehicle_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vehicle_inspections_vehicle_id ON public.vehicle_inspections USING btree (vehicle_id);


--
-- Name: ix_vendor_bill_payments_match_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_bill_payments_match_id ON public.vendor_bill_payments USING btree (match_id);


--
-- Name: ix_vendor_bill_payments_vendor_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_bill_payments_vendor_invoice_id ON public.vendor_bill_payments USING btree (vendor_invoice_id);


--
-- Name: ix_vendor_invoice_lines_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_invoice_lines_job_id ON public.vendor_invoice_lines USING btree (job_id);


--
-- Name: ix_vendor_invoice_lines_vendor_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_invoice_lines_vendor_invoice_id ON public.vendor_invoice_lines USING btree (vendor_invoice_id);


--
-- Name: ix_vendor_invoices_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_invoices_document_id ON public.vendor_invoices USING btree (document_id);


--
-- Name: ix_vendor_invoices_invoice_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_invoices_invoice_number ON public.vendor_invoices USING btree (invoice_number);


--
-- Name: ix_vendor_invoices_matched_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_invoices_matched_job_id ON public.vendor_invoices USING btree (matched_job_id);


--
-- Name: ix_vendor_invoices_po_reference; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_invoices_po_reference ON public.vendor_invoices USING btree (po_reference);


--
-- Name: ix_vendor_invoices_vendor_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_invoices_vendor_id ON public.vendor_invoices USING btree (vendor_id);


--
-- Name: ix_vendor_order_lines_order_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_order_lines_order_id ON public.vendor_order_lines USING btree (order_id);


--
-- Name: ix_vendor_orders_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_orders_account ON public.vendor_orders USING btree (vendor_name, vendor_code, order_date);


--
-- Name: ix_vendor_orders_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_orders_document_id ON public.vendor_orders USING btree (document_id);


--
-- Name: ix_vendor_orders_matched_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_orders_matched_job_id ON public.vendor_orders USING btree (matched_job_id);


--
-- Name: ix_vendor_orders_order_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_orders_order_number ON public.vendor_orders USING btree (order_number);


--
-- Name: ix_vendor_statement_lines_matched_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_statement_lines_matched_job_id ON public.vendor_statement_lines USING btree (matched_job_id);


--
-- Name: ix_vendor_statement_lines_po_ref; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_statement_lines_po_ref ON public.vendor_statement_lines USING btree (po_ref);


--
-- Name: ix_vendor_statement_lines_statement_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_statement_lines_statement_id ON public.vendor_statement_lines USING btree (statement_id);


--
-- Name: ix_vendor_statement_lines_vendor_invoice_no; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_statement_lines_vendor_invoice_no ON public.vendor_statement_lines USING btree (vendor_invoice_no);


--
-- Name: ix_vendor_statements_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_statements_document_id ON public.vendor_statements USING btree (document_id);


--
-- Name: ix_vendors_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendors_name ON public.vendors USING btree (name);


--
-- Name: ix_vendors_qb_vendor_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendors_qb_vendor_id ON public.vendors USING btree (qb_vendor_id);


--
-- Name: ix_webhook_delivery_logs_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_delivery_logs_event ON public.webhook_delivery_logs USING btree (event);


--
-- Name: ix_webhook_delivery_logs_subscription_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_delivery_logs_subscription_id ON public.webhook_delivery_logs USING btree (subscription_id);


--
-- Name: ix_webhook_subscriptions_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_subscriptions_company_id ON public.webhook_subscriptions USING btree (company_id);


--
-- Name: ix_winback_campaigns_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_winback_campaigns_company_id ON public.winback_campaigns USING btree (company_id);


--
-- Name: ix_winback_sends_campaign_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_winback_sends_campaign_id ON public.winback_sends USING btree (campaign_id);


--
-- Name: ix_winback_sends_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_winback_sends_company_id ON public.winback_sends USING btree (company_id);


--
-- Name: ix_winback_sends_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_winback_sends_customer_id ON public.winback_sends USING btree (customer_id);


--
-- Name: uq_bank_match_external_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_bank_match_external_active ON public.bank_match_externals USING btree (source_table, source_id) WHERE ((match_status)::text <> 'rejected'::text);


--
-- Name: uq_bank_match_line_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_bank_match_line_active ON public.bank_match_lines USING btree (line_id) WHERE ((match_status)::text <> 'rejected'::text);


--
-- Name: uq_bank_stmt_import_sha_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_bank_stmt_import_sha_active ON public.bank_statement_imports USING btree (file_sha256) WHERE (voided_at IS NULL);


--
-- Name: uq_gl_accounts_active_system_role; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_gl_accounts_active_system_role ON public.gl_accounts USING btree (company_id, role) WHERE (is_system AND active);


--
-- Name: uq_job_closeouts_live; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_job_closeouts_live ON public.job_closeouts USING btree (job_id) WHERE ((superseded_at IS NULL) AND (deleted_at IS NULL));


--
-- Name: uq_payments_invoice_reference; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_payments_invoice_reference ON public.payments USING btree (invoice_id, reference) WHERE ((reference IS NOT NULL) AND (voided_at IS NULL));


--
-- Name: uq_plugin_email_outbox_delivery; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_plugin_email_outbox_delivery ON public.plugin_email_outbox USING btree (plugin_key, delivery_id);


--
-- Name: uq_vendor_bill_payment_match; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_vendor_bill_payment_match ON public.vendor_bill_payments USING btree (match_id) WHERE ((match_id IS NOT NULL) AND (voided_at IS NULL));


--
-- Name: uq_vendor_invoice_document; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_vendor_invoice_document ON public.vendor_invoices USING btree (document_id) WHERE ((document_id IS NOT NULL) AND (deleted_at IS NULL));


--
-- Name: uq_vendor_invoice_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_vendor_invoice_key ON public.vendor_invoices USING btree (vendor_key, invoice_number) WHERE (deleted_at IS NULL);


--
-- Name: automation_enrollments automation_enrollments_sequence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automation_enrollments
    ADD CONSTRAINT automation_enrollments_sequence_id_fkey FOREIGN KEY (sequence_id) REFERENCES public.automation_sequences(id);


--
-- Name: automation_steps automation_steps_sequence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automation_steps
    ADD CONSTRAINT automation_steps_sequence_id_fkey FOREIGN KEY (sequence_id) REFERENCES public.automation_sequences(id);


--
-- Name: bank_accounts bank_accounts_gl_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_accounts
    ADD CONSTRAINT bank_accounts_gl_account_id_fkey FOREIGN KEY (gl_account_id) REFERENCES public.gl_accounts(id);


--
-- Name: bank_feed_accounts bank_feed_accounts_bank_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_accounts
    ADD CONSTRAINT bank_feed_accounts_bank_account_id_fkey FOREIGN KEY (bank_account_id) REFERENCES public.bank_accounts(id);


--
-- Name: bank_feed_accounts bank_feed_accounts_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_accounts
    ADD CONSTRAINT bank_feed_accounts_connection_id_fkey FOREIGN KEY (connection_id) REFERENCES public.banno_connections(id);


--
-- Name: bank_feed_balance_snapshots bank_feed_balance_snapshots_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_balance_snapshots
    ADD CONSTRAINT bank_feed_balance_snapshots_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.bank_feed_accounts(id);


--
-- Name: bank_feed_documents bank_feed_documents_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_documents
    ADD CONSTRAINT bank_feed_documents_connection_id_fkey FOREIGN KEY (connection_id) REFERENCES public.banno_connections(id);


--
-- Name: bank_feed_transactions bank_feed_transactions_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_feed_transactions
    ADD CONSTRAINT bank_feed_transactions_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.bank_feed_accounts(id);


--
-- Name: bank_match_externals bank_match_externals_match_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_match_externals
    ADD CONSTRAINT bank_match_externals_match_id_fkey FOREIGN KEY (match_id) REFERENCES public.bank_matches(id);


--
-- Name: bank_match_lines bank_match_lines_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_match_lines
    ADD CONSTRAINT bank_match_lines_line_id_fkey FOREIGN KEY (line_id) REFERENCES public.bank_statement_lines(id);


--
-- Name: bank_match_lines bank_match_lines_match_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_match_lines
    ADD CONSTRAINT bank_match_lines_match_id_fkey FOREIGN KEY (match_id) REFERENCES public.bank_matches(id);


--
-- Name: bank_matches bank_matches_bank_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_matches
    ADD CONSTRAINT bank_matches_bank_account_id_fkey FOREIGN KEY (bank_account_id) REFERENCES public.bank_accounts(id);


--
-- Name: bank_statement_imports bank_statement_imports_bank_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_imports
    ADD CONSTRAINT bank_statement_imports_bank_account_id_fkey FOREIGN KEY (bank_account_id) REFERENCES public.bank_accounts(id);


--
-- Name: bank_statement_line_images bank_statement_line_images_import_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_line_images
    ADD CONSTRAINT bank_statement_line_images_import_id_fkey FOREIGN KEY (import_id) REFERENCES public.bank_statement_imports(id);


--
-- Name: bank_statement_line_images bank_statement_line_images_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_line_images
    ADD CONSTRAINT bank_statement_line_images_line_id_fkey FOREIGN KEY (line_id) REFERENCES public.bank_statement_lines(id);


--
-- Name: bank_statement_line_sources bank_statement_line_sources_import_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_line_sources
    ADD CONSTRAINT bank_statement_line_sources_import_id_fkey FOREIGN KEY (import_id) REFERENCES public.bank_statement_imports(id);


--
-- Name: bank_statement_line_sources bank_statement_line_sources_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_line_sources
    ADD CONSTRAINT bank_statement_line_sources_line_id_fkey FOREIGN KEY (line_id) REFERENCES public.bank_statement_lines(id);


--
-- Name: bank_statement_lines bank_statement_lines_bank_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_lines
    ADD CONSTRAINT bank_statement_lines_bank_account_id_fkey FOREIGN KEY (bank_account_id) REFERENCES public.bank_accounts(id);


--
-- Name: bank_statement_lines bank_statement_lines_import_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_lines
    ADD CONSTRAINT bank_statement_lines_import_id_fkey FOREIGN KEY (import_id) REFERENCES public.bank_statement_imports(id);


--
-- Name: banno_connections banno_connections_institution_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banno_connections
    ADD CONSTRAINT banno_connections_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES public.banno_institutions(id);


--
-- Name: change_orders change_orders_billed_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.change_orders
    ADD CONSTRAINT change_orders_billed_invoice_id_fkey FOREIGN KEY (billed_invoice_id) REFERENCES public.invoices(id) ON DELETE SET NULL;


--
-- Name: custom_catalog_items custom_catalog_items_catalog_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.custom_catalog_items
    ADD CONSTRAINT custom_catalog_items_catalog_id_fkey FOREIGN KEY (catalog_id) REFERENCES public.custom_catalogs(id);


--
-- Name: customer_contacts customer_contacts_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_contacts
    ADD CONSTRAINT customer_contacts_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: customer_equipments customer_equipments_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_equipments
    ADD CONSTRAINT customer_equipments_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: customer_users customer_users_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_users
    ADD CONSTRAINT customer_users_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: customer_volume_discount_tiers customer_volume_discount_tiers_settings_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_volume_discount_tiers
    ADD CONSTRAINT customer_volume_discount_tiers_settings_id_fkey FOREIGN KEY (settings_id) REFERENCES public.pricing_settings(id);


--
-- Name: dispatch_routes dispatch_routes_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dispatch_routes
    ADD CONSTRAINT dispatch_routes_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: document_folders document_folders_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_folders
    ADD CONSTRAINT document_folders_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.document_folders(id) ON DELETE RESTRICT;


--
-- Name: documents documents_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: documents documents_estimate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_estimate_id_fkey FOREIGN KEY (estimate_id) REFERENCES public.estimates(id);


--
-- Name: documents documents_folder_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_folder_id_fkey FOREIGN KEY (folder_id) REFERENCES public.document_folders(id);


--
-- Name: documents documents_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: door_listing_photos door_listing_photos_listing_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.door_listing_photos
    ADD CONSTRAINT door_listing_photos_listing_id_fkey FOREIGN KEY (listing_id) REFERENCES public.door_listings(id);


--
-- Name: door_specs door_specs_catalog_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.door_specs
    ADD CONSTRAINT door_specs_catalog_item_id_fkey FOREIGN KEY (catalog_item_id) REFERENCES public.custom_catalog_items(id) ON DELETE CASCADE;


--
-- Name: equipment_service_history equipment_service_history_equipment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.equipment_service_history
    ADD CONSTRAINT equipment_service_history_equipment_id_fkey FOREIGN KEY (equipment_id) REFERENCES public.customer_equipments(id);


--
-- Name: equipment_service_history equipment_service_history_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.equipment_service_history
    ADD CONSTRAINT equipment_service_history_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: estimate_lines estimate_lines_estimate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimate_lines
    ADD CONSTRAINT estimate_lines_estimate_id_fkey FOREIGN KEY (estimate_id) REFERENCES public.estimates(id);


--
-- Name: estimate_lines estimate_lines_labor_price_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimate_lines
    ADD CONSTRAINT estimate_lines_labor_price_item_id_fkey FOREIGN KEY (labor_price_item_id) REFERENCES public.labor_price_items(id) ON DELETE SET NULL;


--
-- Name: estimates estimates_accepted_tier_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimates
    ADD CONSTRAINT estimates_accepted_tier_id_fkey FOREIGN KEY (accepted_tier_id) REFERENCES public.proposal_tiers(id);


--
-- Name: estimates estimates_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimates
    ADD CONSTRAINT estimates_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: estimates estimates_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.estimates
    ADD CONSTRAINT estimates_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: expense_lines expense_lines_expense_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_lines
    ADD CONSTRAINT expense_lines_expense_id_fkey FOREIGN KEY (expense_id) REFERENCES public.expenses(id);


--
-- Name: expense_receipts expense_receipts_expense_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_receipts
    ADD CONSTRAINT expense_receipts_expense_id_fkey FOREIGN KEY (expense_id) REFERENCES public.expenses(id);


--
-- Name: expenses expenses_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: forecast_snapshot_invoices forecast_snapshot_invoices_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.forecast_snapshot_invoices
    ADD CONSTRAINT forecast_snapshot_invoices_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES public.forecast_snapshots(id) ON DELETE CASCADE;


--
-- Name: gl_accounts gl_accounts_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_accounts
    ADD CONSTRAINT gl_accounts_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.gl_accounts(id);


--
-- Name: gl_journal_entries gl_journal_entries_reversed_by_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_journal_entries
    ADD CONSTRAINT gl_journal_entries_reversed_by_entry_id_fkey FOREIGN KEY (reversed_by_entry_id) REFERENCES public.gl_journal_entries(id);


--
-- Name: gl_journal_entries gl_journal_entries_reverses_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_journal_entries
    ADD CONSTRAINT gl_journal_entries_reverses_entry_id_fkey FOREIGN KEY (reverses_entry_id) REFERENCES public.gl_journal_entries(id);


--
-- Name: gl_journal_lines gl_journal_lines_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_journal_lines
    ADD CONSTRAINT gl_journal_lines_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.gl_accounts(id);


--
-- Name: gl_journal_lines gl_journal_lines_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gl_journal_lines
    ADD CONSTRAINT gl_journal_lines_entry_id_fkey FOREIGN KEY (entry_id) REFERENCES public.gl_journal_entries(id);


--
-- Name: hubx_door_orders hubx_door_orders_matched_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hubx_door_orders
    ADD CONSTRAINT hubx_door_orders_matched_job_id_fkey FOREIGN KEY (matched_job_id) REFERENCES public.jobs(id);


--
-- Name: invoice_adjustments invoice_adjustments_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_adjustments
    ADD CONSTRAINT invoice_adjustments_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: invoice_lines invoice_lines_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: invoice_lines invoice_lines_labor_price_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_labor_price_item_id_fkey FOREIGN KEY (labor_price_item_id) REFERENCES public.labor_price_items(id) ON DELETE SET NULL;


--
-- Name: invoice_lines invoice_lines_part_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_part_id_fkey FOREIGN KEY (part_id) REFERENCES public.job_parts_needed(id) ON DELETE SET NULL;


--
-- Name: invoices invoices_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: invoices invoices_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: job_closeouts job_closeouts_billed_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_closeouts
    ADD CONSTRAINT job_closeouts_billed_invoice_id_fkey FOREIGN KEY (billed_invoice_id) REFERENCES public.invoices(id);


--
-- Name: job_closeouts job_closeouts_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_closeouts
    ADD CONSTRAINT job_closeouts_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: job_diagnoses job_diagnoses_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_diagnoses
    ADD CONSTRAINT job_diagnoses_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: job_hazards job_hazards_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_hazards
    ADD CONSTRAINT job_hazards_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: job_hazards job_hazards_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_hazards
    ADD CONSTRAINT job_hazards_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: job_parts job_parts_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_parts
    ADD CONSTRAINT job_parts_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: job_parts_needed job_parts_needed_billed_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_parts_needed
    ADD CONSTRAINT job_parts_needed_billed_invoice_id_fkey FOREIGN KEY (billed_invoice_id) REFERENCES public.invoices(id) ON DELETE SET NULL;


--
-- Name: job_parts job_parts_part_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_parts
    ADD CONSTRAINT job_parts_part_id_fkey FOREIGN KEY (part_id) REFERENCES public.parts(id);


--
-- Name: job_receipts job_receipts_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_receipts
    ADD CONSTRAINT job_receipts_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: jobs jobs_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: jobs jobs_location_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_location_id_fkey FOREIGN KEY (location_id) REFERENCES public.customer_locations(id);


--
-- Name: jobs jobs_parent_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_parent_job_id_fkey FOREIGN KEY (parent_job_id) REFERENCES public.jobs(id);


--
-- Name: margin_tiers margin_tiers_tier_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.margin_tiers
    ADD CONSTRAINT margin_tiers_tier_set_id_fkey FOREIGN KEY (tier_set_id) REFERENCES public.pricing_tier_sets(id);


--
-- Name: outlook_attachments outlook_attachments_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_attachments
    ADD CONSTRAINT outlook_attachments_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.outlook_messages(id);


--
-- Name: outlook_folder_prefs outlook_folder_prefs_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_folder_prefs
    ADD CONSTRAINT outlook_folder_prefs_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.outlook_accounts(id);


--
-- Name: outlook_folder_sync_state outlook_folder_sync_state_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_folder_sync_state
    ADD CONSTRAINT outlook_folder_sync_state_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.outlook_accounts(id);


--
-- Name: outlook_folders outlook_folders_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_folders
    ADD CONSTRAINT outlook_folders_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.outlook_accounts(id);


--
-- Name: outlook_messages outlook_messages_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_messages
    ADD CONSTRAINT outlook_messages_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.outlook_accounts(id);


--
-- Name: outlook_messages outlook_messages_linked_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_messages
    ADD CONSTRAINT outlook_messages_linked_customer_id_fkey FOREIGN KEY (linked_customer_id) REFERENCES public.customers(id);


--
-- Name: outlook_messages outlook_messages_linked_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_messages
    ADD CONSTRAINT outlook_messages_linked_job_id_fkey FOREIGN KEY (linked_job_id) REFERENCES public.jobs(id);


--
-- Name: outlook_subscriptions outlook_subscriptions_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outlook_subscriptions
    ADD CONSTRAINT outlook_subscriptions_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.outlook_accounts(id);


--
-- Name: overhead_obligations overhead_obligations_vendor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.overhead_obligations
    ADD CONSTRAINT overhead_obligations_vendor_id_fkey FOREIGN KEY (vendor_id) REFERENCES public.vendors(id);


--
-- Name: payment_plan_installments payment_plan_installments_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_plan_installments
    ADD CONSTRAINT payment_plan_installments_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.payment_plans(id);


--
-- Name: payment_plans payment_plans_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_plans
    ADD CONSTRAINT payment_plans_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: payments payments_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: phone_com_calls phone_com_calls_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_calls
    ADD CONSTRAINT phone_com_calls_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE SET NULL;


--
-- Name: phone_com_calls phone_com_calls_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_calls
    ADD CONSTRAINT phone_com_calls_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: phone_com_contact_push phone_com_contact_push_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_contact_push
    ADD CONSTRAINT phone_com_contact_push_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE CASCADE;


--
-- Name: phone_com_extensions phone_com_extensions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_extensions
    ADD CONSTRAINT phone_com_extensions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: phone_com_faxes phone_com_faxes_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_faxes
    ADD CONSTRAINT phone_com_faxes_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE SET NULL;


--
-- Name: phone_com_faxes phone_com_faxes_heard_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_faxes
    ADD CONSTRAINT phone_com_faxes_heard_by_user_id_fkey FOREIGN KEY (heard_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: phone_com_faxes phone_com_faxes_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_faxes
    ADD CONSTRAINT phone_com_faxes_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: phone_com_messages phone_com_messages_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_messages
    ADD CONSTRAINT phone_com_messages_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE SET NULL;


--
-- Name: phone_com_messages phone_com_messages_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_messages
    ADD CONSTRAINT phone_com_messages_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: phone_com_messages phone_com_messages_sent_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_messages
    ADD CONSTRAINT phone_com_messages_sent_by_user_id_fkey FOREIGN KEY (sent_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: phone_com_voicemails phone_com_voicemails_call_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_voicemails
    ADD CONSTRAINT phone_com_voicemails_call_id_fkey FOREIGN KEY (call_id) REFERENCES public.phone_com_calls(id) ON DELETE SET NULL;


--
-- Name: phone_com_voicemails phone_com_voicemails_heard_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_com_voicemails
    ADD CONSTRAINT phone_com_voicemails_heard_by_user_id_fkey FOREIGN KEY (heard_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: proposal_tier_lines proposal_tier_lines_tier_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposal_tier_lines
    ADD CONSTRAINT proposal_tier_lines_tier_id_fkey FOREIGN KEY (tier_id) REFERENCES public.proposal_tiers(id);


--
-- Name: proposal_tiers proposal_tiers_estimate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposal_tiers
    ADD CONSTRAINT proposal_tiers_estimate_id_fkey FOREIGN KEY (estimate_id) REFERENCES public.estimates(id);


--
-- Name: purchase_order_lines purchase_order_lines_po_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order_lines
    ADD CONSTRAINT purchase_order_lines_po_id_fkey FOREIGN KEY (po_id) REFERENCES public.purchase_orders(id);


--
-- Name: quote_request_photos quote_request_photos_quote_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quote_request_photos
    ADD CONSTRAINT quote_request_photos_quote_request_id_fkey FOREIGN KEY (quote_request_id) REFERENCES public.quote_requests(id);


--
-- Name: recurring_stream_hits recurring_stream_hits_stream_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recurring_stream_hits
    ADD CONSTRAINT recurring_stream_hits_stream_id_fkey FOREIGN KEY (stream_id) REFERENCES public.recurring_streams(id) ON DELETE CASCADE;


--
-- Name: scheduled_sms scheduled_sms_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_sms
    ADD CONSTRAINT scheduled_sms_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE SET NULL;


--
-- Name: scheduled_sms scheduled_sms_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_sms
    ADD CONSTRAINT scheduled_sms_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: stock_adjustments stock_adjustments_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustments
    ADD CONSTRAINT stock_adjustments_item_id_fkey FOREIGN KEY (item_id) REFERENCES public.inventory_items(id);


--
-- Name: tech_locations tech_locations_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tech_locations
    ADD CONSTRAINT tech_locations_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: time_entries time_entries_billed_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_billed_invoice_id_fkey FOREIGN KEY (billed_invoice_id) REFERENCES public.invoices(id);


--
-- Name: time_entries time_entries_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: timeclocks timeclocks_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.timeclocks
    ADD CONSTRAINT timeclocks_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: user_locations user_locations_location_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_locations
    ADD CONSTRAINT user_locations_location_id_fkey FOREIGN KEY (location_id) REFERENCES public.service_locations(id) ON DELETE CASCADE;


--
-- Name: user_tour_progress user_tour_progress_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tour_progress
    ADD CONSTRAINT user_tour_progress_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: vehicle_service_records vehicle_service_records_vehicle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vehicle_service_records
    ADD CONSTRAINT vehicle_service_records_vehicle_id_fkey FOREIGN KEY (vehicle_id) REFERENCES public.vehicles(id);


--
-- Name: vendor_bill_payments vendor_bill_payments_match_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_bill_payments
    ADD CONSTRAINT vendor_bill_payments_match_id_fkey FOREIGN KEY (match_id) REFERENCES public.bank_matches(id);


--
-- Name: vendor_bill_payments vendor_bill_payments_vendor_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_bill_payments
    ADD CONSTRAINT vendor_bill_payments_vendor_invoice_id_fkey FOREIGN KEY (vendor_invoice_id) REFERENCES public.vendor_invoices(id) ON DELETE CASCADE;


--
-- Name: vendor_invoice_lines vendor_invoice_lines_expense_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_invoice_lines
    ADD CONSTRAINT vendor_invoice_lines_expense_id_fkey FOREIGN KEY (expense_id) REFERENCES public.expenses(id);


--
-- Name: vendor_invoice_lines vendor_invoice_lines_inventory_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_invoice_lines
    ADD CONSTRAINT vendor_invoice_lines_inventory_item_id_fkey FOREIGN KEY (inventory_item_id) REFERENCES public.inventory_items(id);


--
-- Name: vendor_invoice_lines vendor_invoice_lines_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_invoice_lines
    ADD CONSTRAINT vendor_invoice_lines_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: vendor_invoice_lines vendor_invoice_lines_vendor_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_invoice_lines
    ADD CONSTRAINT vendor_invoice_lines_vendor_invoice_id_fkey FOREIGN KEY (vendor_invoice_id) REFERENCES public.vendor_invoices(id) ON DELETE CASCADE;


--
-- Name: vendor_invoices vendor_invoices_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_invoices
    ADD CONSTRAINT vendor_invoices_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: vendor_invoices vendor_invoices_matched_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_invoices
    ADD CONSTRAINT vendor_invoices_matched_job_id_fkey FOREIGN KEY (matched_job_id) REFERENCES public.jobs(id);


--
-- Name: vendor_invoices vendor_invoices_possible_duplicate_of_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_invoices
    ADD CONSTRAINT vendor_invoices_possible_duplicate_of_id_fkey FOREIGN KEY (possible_duplicate_of_id) REFERENCES public.vendor_invoices(id);


--
-- Name: vendor_invoices vendor_invoices_vendor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_invoices
    ADD CONSTRAINT vendor_invoices_vendor_id_fkey FOREIGN KEY (vendor_id) REFERENCES public.vendors(id);


--
-- Name: vendor_order_lines vendor_order_lines_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_order_lines
    ADD CONSTRAINT vendor_order_lines_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.vendor_orders(id) ON DELETE CASCADE;


--
-- Name: vendor_orders vendor_orders_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_orders
    ADD CONSTRAINT vendor_orders_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: vendor_orders vendor_orders_matched_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_orders
    ADD CONSTRAINT vendor_orders_matched_job_id_fkey FOREIGN KEY (matched_job_id) REFERENCES public.jobs(id);


--
-- Name: vendor_statement_lines vendor_statement_lines_matched_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_statement_lines
    ADD CONSTRAINT vendor_statement_lines_matched_job_id_fkey FOREIGN KEY (matched_job_id) REFERENCES public.jobs(id);


--
-- Name: vendor_statement_lines vendor_statement_lines_statement_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_statement_lines
    ADD CONSTRAINT vendor_statement_lines_statement_id_fkey FOREIGN KEY (statement_id) REFERENCES public.vendor_statements(id) ON DELETE CASCADE;


--
-- Name: vendor_statements vendor_statements_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_statements
    ADD CONSTRAINT vendor_statements_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: webhook_deliveries webhook_deliveries_endpoint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_endpoint_id_fkey FOREIGN KEY (endpoint_id) REFERENCES public.webhook_endpoints(id);


--
-- Name: workflow_runs workflow_runs_rule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_runs
    ADD CONSTRAINT workflow_runs_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.workflow_rules(id);


--
-- PostgreSQL database dump complete
--

\unrestrict gdxteststructure

