-- AI Ops Debugger - Agent Queue
-- The queue that the worker daemon polls for new work

CREATE TABLE IF NOT EXISTS ai_ops_agent_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES ai_ops_sessions(id),
    task_type VARCHAR(20) DEFAULT 'bug',
    description TEXT,
    attachments JSONB DEFAULT '[]',
    phase VARCHAR(20) NOT NULL,
    understanding_output TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    priority INTEGER DEFAULT 5,
    picked_up_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result_summary TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_ops_queue_status ON ai_ops_agent_queue(status);
CREATE INDEX IF NOT EXISTS idx_ai_ops_queue_priority ON ai_ops_agent_queue(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_ops_queue_session ON ai_ops_agent_queue(session_id);
