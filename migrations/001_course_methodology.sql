BEGIN;
CREATE TABLE IF NOT EXISTS methodology_evaluations (
    id BIGSERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    call_id BIGINT REFERENCES calls(id) ON DELETE CASCADE,
    training_session_id BIGINT REFERENCES training_sessions(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    transcript_sha256 TEXT NOT NULL,
    legacy_result JSONB,
    status TEXT NOT NULL CHECK (status IN ('processing','complete','failed','uncertain')),
    response_text TEXT,
    result JSONB,
    cost_units NUMERIC(16,4) NOT NULL DEFAULT 0,
    feedback_sent BOOLEAN NOT NULL DEFAULT false,
    feedback_parts_sent INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((call_id IS NOT NULL)::int + (training_session_id IS NOT NULL)::int = 1),
    UNIQUE (call_id, version, transcript_sha256),
    UNIQUE (training_session_id, version, transcript_sha256)
);
CREATE INDEX IF NOT EXISTS methodology_evaluations_client_version_idx
    ON methodology_evaluations (client_id, version, created_at);
CREATE TABLE IF NOT EXISTS methodology_daily_spend (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    day DATE NOT NULL,
    spent_units NUMERIC(16,4) NOT NULL DEFAULT 0,
    PRIMARY KEY (client_id,day)
);
CREATE TABLE IF NOT EXISTS methodology_training_context (
    session_id BIGINT PRIMARY KEY REFERENCES training_sessions(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    scenario JSONB NOT NULL CHECK (jsonb_typeof(scenario) = 'object'),
    telegram_user_id BIGINT NOT NULL,
    turn_state TEXT NOT NULL DEFAULT 'ready' CHECK (turn_state IN ('ready','pending','uncertain')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMIT;
