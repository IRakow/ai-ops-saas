-- Migration 011: Audit log for security and compliance
-- Tracks every significant action across all tenants

CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    actor_type VARCHAR(20) NOT NULL,
    actor_id UUID,
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(50),
    resource_id UUID,
    details JSONB DEFAULT '{}',
    ip_address INET,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_tenant ON audit_log(tenant_id, created_at);
CREATE INDEX idx_audit_action ON audit_log(action, created_at);
