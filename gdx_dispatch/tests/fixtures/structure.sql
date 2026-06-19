--
-- PostgreSQL database dump
--

\restrict aSTBVQEa7ViNlnScGr117PbSNAcaYS0WP1IlROmU32xgezXUNS9scDseEaDfe6G

-- Dumped from database version 15.17
-- Dumped by pg_dump version 15.17

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
-- Name: campaign_channel; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.campaign_channel AS ENUM (
    'sms',
    'email',
    'both'
);


--
-- Name: campaign_send_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.campaign_send_status AS ENUM (
    'pending',
    'sent',
    'failed',
    'cancelled'
);


--
-- Name: campaign_trigger; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.campaign_trigger AS ENUM (
    'estimate_not_accepted',
    'job_completed',
    'manual'
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
    'estimate',
    'scheduled',
    'in_progress',
    'completed',
    'cancelled'
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
-- Name: booking_jobs_router; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.booking_jobs_router (
    id text NOT NULL,
    tenant_id text NOT NULL,
    booking_request_id text NOT NULL,
    created_at text NOT NULL
);


--
-- Name: booking_requests_router; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.booking_requests_router (
    id text NOT NULL,
    tenant_id text NOT NULL,
    name text NOT NULL,
    phone text NOT NULL,
    service text NOT NULL,
    preferred_date text NOT NULL,
    preferred_slot text,
    status text NOT NULL,
    decline_reason text,
    approved_job_id text,
    created_at text NOT NULL,
    updated_at text NOT NULL
);


--
-- Name: bug_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bug_reports (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    user_id character varying(36),
    subject character varying(200) NOT NULL,
    description text NOT NULL,
    priority character varying(20),
    page_url text,
    browser_info text,
    status character varying(20),
    created_at timestamp with time zone,
    resolved_at timestamp with time zone,
    resolved_by character varying(36),
    resolution_notes text
);


--
-- Name: campaign_sends; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.campaign_sends (
    id uuid NOT NULL,
    campaign_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    entity_type character varying(50) NOT NULL,
    entity_id character varying(50) NOT NULL,
    scheduled_at timestamp with time zone NOT NULL,
    sent_at timestamp with time zone,
    status public.campaign_send_status NOT NULL,
    idempotency_key character varying(100) NOT NULL
);


--
-- Name: campaigns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.campaigns (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    trigger public.campaign_trigger NOT NULL,
    delay_days integer NOT NULL,
    message_template text NOT NULL,
    channel public.campaign_channel NOT NULL,
    is_active boolean NOT NULL,
    send_count integer NOT NULL,
    created_at timestamp with time zone NOT NULL
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
    line_total numeric(12,2) NOT NULL
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
    customer_signature_token character varying(255)
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
-- Name: checklist_items_router; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklist_items_router (
    id text NOT NULL,
    tenant_id text NOT NULL,
    checklist_id text NOT NULL,
    item_label text NOT NULL,
    completed integer NOT NULL
);


--
-- Name: checklist_templates_router; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklist_templates_router (
    id text NOT NULL,
    tenant_id text NOT NULL,
    name text NOT NULL,
    items_json text NOT NULL,
    created_at text NOT NULL
);


--
-- Name: checklists_router; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklists_router (
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
    sell_price numeric(10,2)
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
    sell_price numeric(10,2)
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
-- Name: contractor_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contractor_assignments (
    id uuid NOT NULL,
    tenant_id character varying(100),
    contractor_id uuid NOT NULL,
    job_id uuid,
    scheduled_date date NOT NULL,
    hours_worked numeric(10,2),
    total_cost numeric(10,2),
    status character varying(20) NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: contractors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contractors (
    id uuid NOT NULL,
    tenant_id character varying(100),
    name character varying(200) NOT NULL,
    company_name character varying(200),
    phone character varying(30),
    email character varying(200),
    specialty json,
    license_number character varying(100),
    insurance_expiry date,
    hourly_rate numeric(10,2),
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
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
    zip character varying(20)
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
    company_id character varying(36) NOT NULL,
    customer_type character varying(50),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    email_opt_out boolean,
    sms_opt_out boolean
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
    tags character varying(500),
    uploaded_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone,
    size_bytes integer
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
    quantity integer NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    line_total numeric(12,2) NOT NULL,
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
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
    estimate_number character varying(50) NOT NULL,
    label character varying(200),
    notes text,
    proposal_mode boolean NOT NULL,
    total numeric(12,2) NOT NULL,
    status public.estimate_status NOT NULL,
    sent_at timestamp with time zone,
    accepted_at timestamp with time zone,
    declined_at timestamp with time zone,
    declined_reason text,
    accepted_tier_id uuid,
    valid_until timestamp with time zone,
    company_id character varying(36) NOT NULL,
    reminder_sent_at timestamp with time zone,
    public_token character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
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
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    company_id character varying(36) NOT NULL
);


--
-- Name: feature_flags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.feature_flags (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    name character varying(100) NOT NULL,
    enabled boolean NOT NULL,
    description text,
    updated_at timestamp with time zone NOT NULL,
    updated_by character varying(200),
    created_at timestamp with time zone,
    deleted_at timestamp with time zone,
    flag_key character varying(100),
    rollout_pct integer
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
-- Name: inbound_sms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inbound_sms (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    from_number character varying(30) NOT NULL,
    to_number character varying(30) NOT NULL,
    body text NOT NULL,
    provider character varying(30) NOT NULL,
    provider_message_id character varying(100),
    customer_id uuid,
    job_id uuid,
    processed_at timestamp with time zone,
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
-- Name: invoice_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoice_lines (
    id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    description text NOT NULL,
    quantity integer NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    line_total numeric(12,2) NOT NULL,
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    company_id character varying(36) NOT NULL
);


--
-- Name: invoices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoices (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    invoice_number character varying(50) NOT NULL,
    billing_type public.invoice_billing_type NOT NULL,
    sequence_number integer NOT NULL,
    subtotal numeric(12,2) NOT NULL,
    tax_amount numeric(12,2) NOT NULL,
    total numeric(12,2) NOT NULL,
    balance_due numeric(12,2) NOT NULL,
    status public.invoice_status NOT NULL,
    due_date date,
    notes text,
    locked boolean NOT NULL,
    locked_at timestamp with time zone,
    sent_at timestamp with time zone,
    paid_at timestamp with time zone,
    public_token character varying(64) NOT NULL,
    amount_paid numeric(12,2),
    customer_id uuid,
    company_id character varying(36) NOT NULL,
    total_amount numeric(12,2),
    invoice_date date,
    created_at timestamp with time zone NOT NULL,
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
    uploaded_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    content_type character varying(150),
    created_at timestamp with time zone,
    file_size integer
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
    title character varying(200) NOT NULL,
    description text,
    lifecycle_stage public.job_lifecycle_stage DEFAULT 'lead'::public.job_lifecycle_stage NOT NULL,
    dispatch_status public.job_dispatch_status NOT NULL,
    billing_status public.job_billing_status DEFAULT 'unbilled'::public.job_billing_status NOT NULL,
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
    deleted_at timestamp with time zone,
    approved_at timestamp with time zone,
    arrived_at timestamp with time zone,
    dispatched_at timestamp with time zone,
    holding_area_id character varying(36),
    notes text,
    signature_data text,
    signed_at timestamp with time zone,
    signed_by character varying(200)
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
-- Name: mobile_sync_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mobile_sync_actions (
    id text NOT NULL,
    company_id text NOT NULL,
    fingerprint text,
    action_type text,
    entity_id text,
    queued_at text,
    created_at text
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
    created_at text NOT NULL
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
    payment_date date NOT NULL,
    created_at timestamp with time zone NOT NULL,
    company_id character varying(36) NOT NULL
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
    created_at timestamp with time zone,
    completed_at timestamp with time zone
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
-- Name: po_request_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.po_request_lines (
    id uuid NOT NULL,
    po_id uuid NOT NULL,
    sku character varying(100),
    name character varying(300) NOT NULL,
    quantity integer NOT NULL,
    unit_price numeric(12,2) NOT NULL
);


--
-- Name: po_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.po_requests (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    requested_by character varying(36) NOT NULL,
    job_id character varying(36),
    customer_id character varying(36),
    supplier_name character varying(300),
    status character varying(30) NOT NULL,
    notes text,
    created_at timestamp with time zone,
    approved_at timestamp with time zone,
    received_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: portal_booking_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_booking_requests (
    id text NOT NULL,
    customer_id text NOT NULL,
    requested_date text NOT NULL,
    service_type text NOT NULL,
    notes text,
    status text NOT NULL,
    created_at text NOT NULL
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
-- Name: proposals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proposals (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    customer_id uuid,
    customer_name character varying(200),
    title character varying(300) NOT NULL,
    description text,
    good_price numeric(12,2) NOT NULL,
    better_price numeric(12,2) NOT NULL,
    best_price numeric(12,2) NOT NULL,
    good_description text,
    better_description text,
    best_description text,
    status character varying(30) NOT NULL,
    chosen_tier character varying(10),
    sent_at timestamp with time zone,
    accepted_at timestamp with time zone,
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
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
-- Name: qb_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_accounts (
    id character varying(36) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    qb_account_id character varying(120) NOT NULL,
    name character varying(300) NOT NULL,
    account_type character varying(100),
    account_sub_type character varying(100),
    classification character varying(100),
    current_balance numeric(14,2),
    active boolean,
    synced_at timestamp with time zone
);


--
-- Name: qb_bank_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_bank_transactions (
    id character varying(36) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    qb_txn_id character varying(120) NOT NULL,
    txn_date date,
    txn_type character varying(50),
    account_name character varying(300),
    payee character varying(300),
    amount numeric(14,2),
    memo text,
    category character varying(300),
    status character varying(50),
    synced_at timestamp with time zone
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
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
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
-- Name: qb_token_store; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qb_token_store (
    id uuid NOT NULL,
    realm_id character varying(50) NOT NULL,
    access_token_enc text NOT NULL,
    refresh_token_enc text NOT NULL,
    access_token_expires_at timestamp with time zone NOT NULL,
    refresh_token_expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
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
-- Name: reminder_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reminder_settings (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    enabled boolean NOT NULL,
    schedule_days text NOT NULL,
    subject_template character varying(500) NOT NULL,
    body_template text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
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
-- Name: review_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.review_requests (
    id text NOT NULL,
    job_id text,
    customer_id text,
    status text NOT NULL,
    message text,
    google_reviews_link text,
    scheduled_for text,
    sent_at text,
    created_at text
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
-- Name: saas_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.saas_subscriptions (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    plan character varying(50) NOT NULL,
    status character varying(30) NOT NULL,
    stripe_customer_id character varying(120),
    stripe_subscription_id character varying(120),
    trial_ends_at timestamp with time zone,
    current_period_end timestamp with time zone,
    monthly_price_cents integer NOT NULL,
    seat_count integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: safety_checklists; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.safety_checklists (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    job_id character varying(36) NOT NULL,
    technician_id character varying(36) NOT NULL,
    items text NOT NULL,
    completed boolean,
    photo_url text,
    signed_at timestamp with time zone,
    created_at timestamp with time zone,
    deleted_at timestamp with time zone
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
    deleted_at timestamp with time zone NOT NULL
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
-- Name: sticky_notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sticky_notes (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    title character varying(200),
    body text NOT NULL,
    color character varying(20) NOT NULL,
    pos_x integer NOT NULL,
    pos_y integer NOT NULL,
    width integer NOT NULL,
    height integer NOT NULL,
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
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
-- Name: supplier_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.supplier_accounts (
    id uuid NOT NULL,
    email character varying(254) NOT NULL,
    password_hash character varying(256) NOT NULL,
    company_name character varying(200) NOT NULL,
    phone character varying(50),
    created_at timestamp with time zone
);


--
-- Name: supplier_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.supplier_catalog (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    supplier_name character varying(200) NOT NULL,
    sku character varying(100),
    name character varying(300) NOT NULL,
    description text,
    unit_price numeric(12,2) NOT NULL,
    stock_level integer,
    category character varying(100),
    created_at timestamp with time zone
);


--
-- Name: supplier_invitations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.supplier_invitations (
    id uuid NOT NULL,
    tenant_id character varying(36) NOT NULL,
    supplier_email character varying(254) NOT NULL,
    supplier_name character varying(200) NOT NULL,
    token character varying(100) NOT NULL,
    status character varying(20) NOT NULL,
    created_at timestamp with time zone,
    accepted_at timestamp with time zone
);


--
-- Name: supplier_order_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.supplier_order_lines (
    id uuid NOT NULL,
    order_id uuid NOT NULL,
    sku character varying(100),
    name character varying(300),
    quantity integer NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    line_total numeric(12,2) NOT NULL
);


--
-- Name: supplier_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.supplier_orders (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    supplier_name character varying(200) NOT NULL,
    status character varying(50) NOT NULL,
    total_amount numeric(12,2) NOT NULL,
    notes text,
    created_at timestamp with time zone,
    updated_at timestamp with time zone
);


--
-- Name: supplier_tenant_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.supplier_tenant_links (
    id uuid NOT NULL,
    supplier_id uuid NOT NULL,
    tenant_id character varying(36) NOT NULL,
    status character varying(20) NOT NULL,
    created_at timestamp with time zone
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
-- Name: tax_jurisdictions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_jurisdictions (
    id character varying(36) NOT NULL,
    company_id character varying(36) NOT NULL,
    name character varying(200) NOT NULL,
    rate numeric(6,4) NOT NULL,
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
-- Name: tech_unavailability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tech_unavailability (
    id uuid NOT NULL,
    company_id character varying(64) NOT NULL,
    tech_id character varying(64) NOT NULL,
    start_at timestamp with time zone NOT NULL,
    end_at timestamp with time zone NOT NULL,
    reason character varying(200),
    created_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone
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
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id character varying(36) NOT NULL,
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
    mcp_enabled boolean
);


--
-- Name: van_inventory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.van_inventory (
    id uuid NOT NULL,
    company_id character varying(36) NOT NULL,
    truck_id character varying(36) NOT NULL,
    sku character varying(100),
    name character varying(300) NOT NULL,
    quantity integer NOT NULL,
    min_stock integer NOT NULL,
    category character varying(100),
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: van_inventory_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.van_inventory_log (
    id uuid NOT NULL,
    van_inventory_id uuid NOT NULL,
    job_id character varying(36),
    quantity_change integer NOT NULL,
    reason character varying(500),
    created_by character varying(36) NOT NULL,
    created_at timestamp with time zone
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
-- Name: vendors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendors (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    account_number character varying(100),
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
    tax_id character varying(50),
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
-- Name: booking_jobs_router booking_jobs_router_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.booking_jobs_router
    ADD CONSTRAINT booking_jobs_router_pkey PRIMARY KEY (id);


--
-- Name: booking_requests_router booking_requests_router_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.booking_requests_router
    ADD CONSTRAINT booking_requests_router_pkey PRIMARY KEY (id);


--
-- Name: bug_reports bug_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bug_reports
    ADD CONSTRAINT bug_reports_pkey PRIMARY KEY (id);


--
-- Name: campaign_sends campaign_sends_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campaign_sends
    ADD CONSTRAINT campaign_sends_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: campaign_sends campaign_sends_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campaign_sends
    ADD CONSTRAINT campaign_sends_pkey PRIMARY KEY (id);


--
-- Name: campaigns campaigns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campaigns
    ADD CONSTRAINT campaigns_pkey PRIMARY KEY (id);


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
-- Name: checklist_items_router checklist_items_router_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_items_router
    ADD CONSTRAINT checklist_items_router_pkey PRIMARY KEY (id);


--
-- Name: checklist_templates_router checklist_templates_router_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_templates_router
    ADD CONSTRAINT checklist_templates_router_pkey PRIMARY KEY (id);


--
-- Name: checklists_router checklists_router_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklists_router
    ADD CONSTRAINT checklists_router_pkey PRIMARY KEY (id);


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
-- Name: contractor_assignments contractor_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contractor_assignments
    ADD CONSTRAINT contractor_assignments_pkey PRIMARY KEY (id);


--
-- Name: contractors contractors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contractors
    ADD CONSTRAINT contractors_pkey PRIMARY KEY (id);


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
-- Name: expenses expenses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_pkey PRIMARY KEY (id);


--
-- Name: feature_flags feature_flags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feature_flags
    ADD CONSTRAINT feature_flags_pkey PRIMARY KEY (id);


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
-- Name: gdpr_data_access_logs gdpr_data_access_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gdpr_data_access_logs
    ADD CONSTRAINT gdpr_data_access_logs_pkey PRIMARY KEY (id);


--
-- Name: holding_areas holding_areas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holding_areas
    ADD CONSTRAINT holding_areas_pkey PRIMARY KEY (id);


--
-- Name: inbound_emails inbound_emails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbound_emails
    ADD CONSTRAINT inbound_emails_pkey PRIMARY KEY (id);


--
-- Name: inbound_sms inbound_sms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbound_sms
    ADD CONSTRAINT inbound_sms_pkey PRIMARY KEY (id);


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
-- Name: job_dependencies job_dependencies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_dependencies
    ADD CONSTRAINT job_dependencies_pkey PRIMARY KEY (id);


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
-- Name: mobile_sync_actions mobile_sync_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mobile_sync_actions
    ADD CONSTRAINT mobile_sync_actions_pkey PRIMARY KEY (id);


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
-- Name: po_request_lines po_request_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_request_lines
    ADD CONSTRAINT po_request_lines_pkey PRIMARY KEY (id);


--
-- Name: po_requests po_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_requests
    ADD CONSTRAINT po_requests_pkey PRIMARY KEY (id);


--
-- Name: portal_booking_requests portal_booking_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_booking_requests
    ADD CONSTRAINT portal_booking_requests_pkey PRIMARY KEY (id);


--
-- Name: portal_messages portal_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_messages
    ADD CONSTRAINT portal_messages_pkey PRIMARY KEY (id);


--
-- Name: pricing_tiers pricing_tiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pricing_tiers
    ADD CONSTRAINT pricing_tiers_pkey PRIMARY KEY (id);


--
-- Name: proposal_tiers proposal_tiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposal_tiers
    ADD CONSTRAINT proposal_tiers_pkey PRIMARY KEY (id);


--
-- Name: proposals proposals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT proposals_pkey PRIMARY KEY (id);


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
-- Name: qb_accounts qb_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_accounts
    ADD CONSTRAINT qb_accounts_pkey PRIMARY KEY (id);


--
-- Name: qb_accounts qb_accounts_tenant_id_qb_account_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_accounts
    ADD CONSTRAINT qb_accounts_tenant_id_qb_account_id_key UNIQUE (tenant_id, qb_account_id);


--
-- Name: qb_bank_transactions qb_bank_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_bank_transactions
    ADD CONSTRAINT qb_bank_transactions_pkey PRIMARY KEY (id);


--
-- Name: qb_bank_transactions qb_bank_transactions_tenant_id_qb_txn_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_bank_transactions
    ADD CONSTRAINT qb_bank_transactions_tenant_id_qb_txn_id_key UNIQUE (tenant_id, qb_txn_id);


--
-- Name: qb_connections qb_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_connections
    ADD CONSTRAINT qb_connections_pkey PRIMARY KEY (id);


--
-- Name: qb_entity_maps qb_entity_maps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_entity_maps
    ADD CONSTRAINT qb_entity_maps_pkey PRIMARY KEY (id);


--
-- Name: qb_token_store qb_token_store_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_token_store
    ADD CONSTRAINT qb_token_store_pkey PRIMARY KEY (id);


--
-- Name: qb_token_store qb_token_store_realm_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_token_store
    ADD CONSTRAINT qb_token_store_realm_id_key UNIQUE (realm_id);


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
-- Name: reminder_settings reminder_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reminder_settings
    ADD CONSTRAINT reminder_settings_pkey PRIMARY KEY (id);


--
-- Name: resources resources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resources
    ADD CONSTRAINT resources_pkey PRIMARY KEY (id);


--
-- Name: review_requests review_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_requests
    ADD CONSTRAINT review_requests_pkey PRIMARY KEY (id);


--
-- Name: role_permissions role_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (role);


--
-- Name: saas_subscriptions saas_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saas_subscriptions
    ADD CONSTRAINT saas_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: safety_checklists safety_checklists_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.safety_checklists
    ADD CONSTRAINT safety_checklists_pkey PRIMARY KEY (id);


--
-- Name: saved_reports saved_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_reports
    ADD CONSTRAINT saved_reports_pkey PRIMARY KEY (id);


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
-- Name: service_triggers service_triggers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_triggers
    ADD CONSTRAINT service_triggers_pkey PRIMARY KEY (id);


--
-- Name: sticky_notes sticky_notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sticky_notes
    ADD CONSTRAINT sticky_notes_pkey PRIMARY KEY (id);


--
-- Name: stock_adjustments stock_adjustments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustments
    ADD CONSTRAINT stock_adjustments_pkey PRIMARY KEY (id);


--
-- Name: supplier_accounts supplier_accounts_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.supplier_accounts
    ADD CONSTRAINT supplier_accounts_email_key UNIQUE (email);


--
-- Name: supplier_accounts supplier_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.supplier_accounts
    ADD CONSTRAINT supplier_accounts_pkey PRIMARY KEY (id);


--
-- Name: supplier_catalog supplier_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.supplier_catalog
    ADD CONSTRAINT supplier_catalog_pkey PRIMARY KEY (id);


--
-- Name: supplier_invitations supplier_invitations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.supplier_invitations
    ADD CONSTRAINT supplier_invitations_pkey PRIMARY KEY (id);


--
-- Name: supplier_invitations supplier_invitations_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.supplier_invitations
    ADD CONSTRAINT supplier_invitations_token_key UNIQUE (token);


--
-- Name: supplier_order_lines supplier_order_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.supplier_order_lines
    ADD CONSTRAINT supplier_order_lines_pkey PRIMARY KEY (id);


--
-- Name: supplier_orders supplier_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.supplier_orders
    ADD CONSTRAINT supplier_orders_pkey PRIMARY KEY (id);


--
-- Name: supplier_tenant_links supplier_tenant_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.supplier_tenant_links
    ADD CONSTRAINT supplier_tenant_links_pkey PRIMARY KEY (id);


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
-- Name: tech_unavailability tech_unavailability_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tech_unavailability
    ADD CONSTRAINT tech_unavailability_pkey PRIMARY KEY (id);


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
-- Name: feature_flags uq_feature_flags_company_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feature_flags
    ADD CONSTRAINT uq_feature_flags_company_name UNIQUE (company_id, name);


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
-- Name: pdf_templates uq_pdf_template; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pdf_templates
    ADD CONSTRAINT uq_pdf_template UNIQUE (company_id, template_type);


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
-- Name: qb_vendors uq_qb_vendor_tenant; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qb_vendors
    ADD CONSTRAINT uq_qb_vendor_tenant UNIQUE (tenant_id, qb_vendor_id);


--
-- Name: supplier_tenant_links uq_supplier_tenant; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.supplier_tenant_links
    ADD CONSTRAINT uq_supplier_tenant UNIQUE (supplier_id, tenant_id);


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
-- Name: user_role_assignments uq_user_role_assignment; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_role_assignments
    ADD CONSTRAINT uq_user_role_assignment UNIQUE (company_id, user_id, role_id);


--
-- Name: user_role_assignments user_role_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_role_assignments
    ADD CONSTRAINT user_role_assignments_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: van_inventory_log van_inventory_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.van_inventory_log
    ADD CONSTRAINT van_inventory_log_pkey PRIMARY KEY (id);


--
-- Name: van_inventory van_inventory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.van_inventory
    ADD CONSTRAINT van_inventory_pkey PRIMARY KEY (id);


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
-- Name: ix_catalog_items_sku; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_catalog_items_sku ON public.catalog_items USING btree (sku);


--
-- Name: ix_catalog_items_wholesaler_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_catalog_items_wholesaler_tenant_id ON public.catalog_items USING btree (wholesaler_tenant_id);


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
-- Name: ix_custom_catalog_items_catalog_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_catalog_items_catalog_id ON public.custom_catalog_items USING btree (catalog_id);


--
-- Name: ix_custom_catalog_items_qb_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_custom_catalog_items_qb_item_id ON public.custom_catalog_items USING btree (qb_item_id);


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
-- Name: ix_feature_flags_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_feature_flags_company_id ON public.feature_flags USING btree (company_id);


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
-- Name: ix_inbound_sms_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inbound_sms_company_id ON public.inbound_sms USING btree (company_id);


--
-- Name: ix_inbound_sms_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inbound_sms_customer_id ON public.inbound_sms USING btree (customer_id);


--
-- Name: ix_inbound_sms_from_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inbound_sms_from_number ON public.inbound_sms USING btree (from_number);


--
-- Name: ix_inbound_sms_provider_message_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inbound_sms_provider_message_id ON public.inbound_sms USING btree (provider_message_id);


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
-- Name: ix_job_notes_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_notes_company_id ON public.job_notes USING btree (company_id);


--
-- Name: ix_job_notes_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_notes_job_id ON public.job_notes USING btree (job_id);


--
-- Name: ix_job_photos_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_photos_company_id ON public.job_photos USING btree (company_id);


--
-- Name: ix_job_photos_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_photos_job_id ON public.job_photos USING btree (job_id);


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
-- Name: ix_markup_rules_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_markup_rules_company_id ON public.markup_rules USING btree (company_id);


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
-- Name: ix_part_prices_part_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_part_prices_part_number ON public.part_prices USING btree (part_number);


--
-- Name: ix_part_prices_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_part_prices_tenant_id ON public.part_prices USING btree (tenant_id);


--
-- Name: ix_payment_reminders_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payment_reminders_invoice_id ON public.payment_reminders USING btree (invoice_id);


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
-- Name: ix_pricing_tiers_distributor_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pricing_tiers_distributor_tenant_id ON public.pricing_tiers USING btree (distributor_tenant_id);


--
-- Name: ix_pricing_tiers_wholesaler_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pricing_tiers_wholesaler_tenant_id ON public.pricing_tiers USING btree (wholesaler_tenant_id);


--
-- Name: ix_proposals_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proposals_company_id ON public.proposals USING btree (company_id);


--
-- Name: ix_proposals_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proposals_customer_id ON public.proposals USING btree (customer_id);


--
-- Name: ix_purchase_order_lines_po_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_purchase_order_lines_po_id ON public.purchase_order_lines USING btree (po_id);


--
-- Name: ix_purchase_orders_po_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_purchase_orders_po_number ON public.purchase_orders USING btree (po_number);


--
-- Name: ix_purchase_orders_vendor_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_purchase_orders_vendor_id ON public.purchase_orders USING btree (vendor_id);


--
-- Name: ix_qb_connections_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_qb_connections_tenant_id ON public.qb_connections USING btree (tenant_id);


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
-- Name: ix_qb_vendors_qb_vendor_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_vendors_qb_vendor_id ON public.qb_vendors USING btree (qb_vendor_id);


--
-- Name: ix_qb_vendors_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qb_vendors_tenant_id ON public.qb_vendors USING btree (tenant_id);


--
-- Name: ix_quote_templates_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_quote_templates_tenant_id ON public.quote_templates USING btree (tenant_id);


--
-- Name: ix_reminder_settings_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_reminder_settings_company_id ON public.reminder_settings USING btree (company_id);


--
-- Name: ix_saas_subscriptions_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_saas_subscriptions_company_id ON public.saas_subscriptions USING btree (company_id);


--
-- Name: ix_saas_subscriptions_stripe_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_saas_subscriptions_stripe_customer_id ON public.saas_subscriptions USING btree (stripe_customer_id);


--
-- Name: ix_saas_subscriptions_stripe_subscription_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_saas_subscriptions_stripe_subscription_id ON public.saas_subscriptions USING btree (stripe_subscription_id);


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
-- Name: ix_sticky_notes_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sticky_notes_company_id ON public.sticky_notes USING btree (company_id);


--
-- Name: ix_stock_adjustments_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_stock_adjustments_item_id ON public.stock_adjustments USING btree (item_id);


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
-- Name: ix_tech_unavailability_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tech_unavailability_company_id ON public.tech_unavailability USING btree (company_id);


--
-- Name: ix_tech_unavailability_tech_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tech_unavailability_tech_id ON public.tech_unavailability USING btree (tech_id);


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
-- Name: campaign_sends campaign_sends_campaign_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campaign_sends
    ADD CONSTRAINT campaign_sends_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES public.campaigns(id);


--
-- Name: campaign_sends campaign_sends_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campaign_sends
    ADD CONSTRAINT campaign_sends_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: contractor_assignments contractor_assignments_contractor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contractor_assignments
    ADD CONSTRAINT contractor_assignments_contractor_id_fkey FOREIGN KEY (contractor_id) REFERENCES public.contractors(id);


--
-- Name: custom_catalog_items custom_catalog_items_catalog_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.custom_catalog_items
    ADD CONSTRAINT custom_catalog_items_catalog_id_fkey FOREIGN KEY (catalog_id) REFERENCES public.custom_catalogs(id);


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
-- Name: dispatch_routes dispatch_routes_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dispatch_routes
    ADD CONSTRAINT dispatch_routes_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: documents documents_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


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
-- Name: expenses expenses_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: invoice_lines invoice_lines_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


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
-- Name: job_parts job_parts_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_parts
    ADD CONSTRAINT job_parts_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: job_parts job_parts_part_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_parts
    ADD CONSTRAINT job_parts_part_id_fkey FOREIGN KEY (part_id) REFERENCES public.parts(id);


--
-- Name: jobs jobs_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: jobs jobs_parent_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_parent_job_id_fkey FOREIGN KEY (parent_job_id) REFERENCES public.jobs(id);


--
-- Name: payments payments_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


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
-- Name: stock_adjustments stock_adjustments_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustments
    ADD CONSTRAINT stock_adjustments_item_id_fkey FOREIGN KEY (item_id) REFERENCES public.inventory_items(id);


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
-- Name: vehicle_service_records vehicle_service_records_vehicle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vehicle_service_records
    ADD CONSTRAINT vehicle_service_records_vehicle_id_fkey FOREIGN KEY (vehicle_id) REFERENCES public.vehicles(id);


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

\unrestrict aSTBVQEa7ViNlnScGr117PbSNAcaYS0WP1IlROmU32xgezXUNS9scDseEaDfe6G

