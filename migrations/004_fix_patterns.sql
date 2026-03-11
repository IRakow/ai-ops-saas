-- AI Ops Debugger - Fix Patterns (Knowledge Base)
-- Records what worked/failed so agents learn over time

CREATE TABLE IF NOT EXISTS ai_ops_fix_patterns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES ai_ops_sessions(id),
    error_signature VARCHAR(500),
    error_category VARCHAR(100),
    fix_approach TEXT,
    fix_result VARCHAR(50),
    files_modified JSONB DEFAULT '[]',
    confidence FLOAT DEFAULT 0.5,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_ops_fix_patterns_signature ON ai_ops_fix_patterns(error_signature);
CREATE INDEX IF NOT EXISTS idx_ai_ops_fix_patterns_category ON ai_ops_fix_patterns(error_category);
CREATE INDEX IF NOT EXISTS idx_ai_ops_fix_patterns_result ON ai_ops_fix_patterns(fix_result);
