-- Migration 005: Tenants table
-- Multi-tenant SaaS: one row per client organization

CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) NOT NULL UNIQUE,
    plan VARCHAR(20) NOT NULL DEFAULT 'trial',
    status VARCHAR(20) NOT NULL DEFAULT 'trial',

    -- Git access
    git_repo_url TEXT,
    git_provider VARCHAR(20) DEFAULT 'github',
    git_credentials_encrypted TEXT,
    git_default_branch VARCHAR(100) DEFAULT 'main',
    git_deploy_branch VARCHAR(100) DEFAULT 'main',

    -- Workspace
    workspace_path TEXT,
    last_git_sync TIMESTAMPTZ,

    -- Codebase context (loaded into agent prompts)
    codebase_context TEXT,
    blast_radius JSONB DEFAULT '{}',
    agent_protocol TEXT,
    manifest JSONB DEFAULT '{}',

    -- App info
    app_name VARCHAR(255),
    app_description TEXT,
    app_url TEXT,
    app_stack TEXT,

    -- Deploy
    deploy_method VARCHAR(20) DEFAULT 'github_pr',
    deploy_config JSONB DEFAULT '{}',

    -- Notifications
    notification_emails TEXT[] DEFAULT '{}',
    notification_webhook_url TEXT,
    notification_slack_webhook TEXT,

    -- Billing (Valor Payment Systems)
    valor_customer_id VARCHAR(255),
    valor_subscription_id VARCHAR(255),
    billing_email VARCHAR(255),
    monthly_fix_limit INTEGER DEFAULT 10,
    monthly_feature_limit INTEGER DEFAULT 2,

    -- API
    api_key_hash VARCHAR(255) UNIQUE,
    api_key_prefix VARCHAR(12),

    -- Metadata
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    trial_ends_at TIMESTAMPTZ,
    onboarded_at TIMESTAMPTZ
);

CREATE INDEX idx_tenants_slug ON tenants(slug);
CREATE INDEX idx_tenants_api_key_prefix ON tenants(api_key_prefix);
CREATE INDEX idx_tenants_status ON tenants(status);
