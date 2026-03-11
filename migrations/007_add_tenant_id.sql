-- Migration 007: Add tenant_id to all existing AI Ops tables
-- This converts the single-tenant schema to multi-tenant

ALTER TABLE ai_ops_users ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_sessions ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_messages ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_tasks ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_files ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_audit_log ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_agent_queue ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_notes ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_note_suggestions ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_fix_patterns ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);

-- Indexes for tenant-scoped queries
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON ai_ops_sessions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_queue_tenant ON ai_ops_agent_queue(tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON ai_ops_users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_fix_patterns_tenant ON ai_ops_fix_patterns(tenant_id);
CREATE INDEX IF NOT EXISTS idx_notes_tenant ON ai_ops_notes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_messages_tenant ON ai_ops_messages(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON ai_ops_tasks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_files_tenant ON ai_ops_files(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_tenant ON ai_ops_audit_log(tenant_id);
