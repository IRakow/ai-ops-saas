-- AI Ops Debugger - Notes & Feedback
-- User notes and AI-generated suggestions

CREATE TABLE IF NOT EXISTS ai_ops_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES ai_ops_users(id),
    session_id UUID REFERENCES ai_ops_sessions(id),
    content TEXT NOT NULL,
    category VARCHAR(50) DEFAULT 'general',
    priority VARCHAR(20) DEFAULT 'medium',
    status VARCHAR(20) DEFAULT 'open',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_ops_note_suggestions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    note_id UUID NOT NULL REFERENCES ai_ops_notes(id) ON DELETE CASCADE,
    suggestion TEXT NOT NULL,
    suggestion_type VARCHAR(50),
    confidence FLOAT,
    accepted BOOLEAN DEFAULT FALSE,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_ops_notes_user ON ai_ops_notes(user_id);
CREATE INDEX IF NOT EXISTS idx_ai_ops_notes_session ON ai_ops_notes(session_id);
CREATE INDEX IF NOT EXISTS idx_ai_ops_note_suggestions_note ON ai_ops_note_suggestions(note_id);
