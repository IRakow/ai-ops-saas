-- Migration 009: Usage tracking for billing
-- One record per agent pipeline run

CREATE TABLE usage_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    session_id UUID REFERENCES ai_ops_sessions(id),

    record_type VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'started',

    -- Token usage
    input_tokens BIGINT DEFAULT 0,
    output_tokens BIGINT DEFAULT 0,
    total_cost_cents INTEGER DEFAULT 0,

    -- Timing
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_seconds INTEGER,

    -- Agent details
    agents_used JSONB DEFAULT '[]',
    retries INTEGER DEFAULT 0,
    verdict VARCHAR(20),

    -- Billing
    billed BOOLEAN DEFAULT FALSE,
    billed_at TIMESTAMPTZ,
    invoice_id VARCHAR(255),

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_usage_tenant ON usage_records(tenant_id);
CREATE INDEX idx_usage_tenant_month ON usage_records(tenant_id, created_at);
CREATE INDEX idx_usage_unbilled ON usage_records(billed) WHERE billed = FALSE;
