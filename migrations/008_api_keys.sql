-- Migration 008: Per-tenant API keys
-- Each tenant can have multiple keys with different scopes

CREATE TABLE tenant_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    key_hash VARCHAR(255) NOT NULL UNIQUE,
    key_prefix VARCHAR(12) NOT NULL,
    scopes TEXT[] DEFAULT '{intake,read}',
    is_active BOOLEAN DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX idx_api_keys_hash ON tenant_api_keys(key_hash);
CREATE INDEX idx_api_keys_tenant ON tenant_api_keys(tenant_id);
