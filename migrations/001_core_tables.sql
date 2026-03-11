-- AI Ops Debugger - Core Tables
-- Run this first against your Supabase project

-- Users who can access the AI Ops UI
CREATE TABLE IF NOT EXISTS ai_ops_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) DEFAULT 'user',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Debug sessions (one per bug report or feature request)
CREATE TABLE IF NOT EXISTS ai_ops_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES ai_ops_users(id),
    title VARCHAR(500),
    description TEXT,
    task_type VARCHAR(20) DEFAULT 'bug',
    status VARCHAR(50) DEFAULT 'open',
    phase VARCHAR(50) DEFAULT 'intake',
    summary TEXT,
    understanding_output TEXT,
    plan_output TEXT,
    implementation_output TEXT,
    test_output TEXT,
    assessment_output TEXT,
    verdict VARCHAR(50),
    retry_count INTEGER DEFAULT 0,
    attachments JSONB DEFAULT '[]',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Chat messages within a session
CREATE TABLE IF NOT EXISTS ai_ops_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES ai_ops_sessions(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    agent_name VARCHAR(100),
    phase VARCHAR(50),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Tasks spawned by agents within a session
CREATE TABLE IF NOT EXISTS ai_ops_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES ai_ops_sessions(id) ON DELETE CASCADE,
    task_type VARCHAR(50) NOT NULL,
    status VARCHAR(50) DEFAULT 'pending',
    description TEXT,
    result TEXT,
    agent_name VARCHAR(100),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- Files modified by agents
CREATE TABLE IF NOT EXISTS ai_ops_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES ai_ops_sessions(id) ON DELETE CASCADE,
    file_path VARCHAR(1000) NOT NULL,
    action VARCHAR(50) NOT NULL,
    diff TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Audit log for all significant actions
CREATE TABLE IF NOT EXISTS ai_ops_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES ai_ops_sessions(id),
    user_id UUID REFERENCES ai_ops_users(id),
    action VARCHAR(100) NOT NULL,
    details JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_ai_ops_sessions_user ON ai_ops_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_ai_ops_sessions_status ON ai_ops_sessions(status);
CREATE INDEX IF NOT EXISTS idx_ai_ops_messages_session ON ai_ops_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_ai_ops_messages_created ON ai_ops_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_ops_tasks_session ON ai_ops_tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_ai_ops_audit_log_session ON ai_ops_audit_log(session_id);
