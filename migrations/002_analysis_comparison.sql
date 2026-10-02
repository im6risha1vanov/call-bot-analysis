-- Isolated experiments: no UPDATE/ALTER of production analysis or configuration.
CREATE TABLE IF NOT EXISTS analysis_comparisons (
 id BIGSERIAL PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
 initiator BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'queued'
 CHECK(status IN ('queued','running','ready','partial','failed')),
 packages JSONB NOT NULL, settings JSONB NOT NULL,
 revealed_at TIMESTAMPTZ, pending_comment_ordinal INTEGER,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS analysis_comparison_running ON analysis_comparisons(client_id,initiator)
 WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS analysis_comparison_calls (
 comparison_id BIGINT NOT NULL REFERENCES analysis_comparisons(id), ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 2),
 call_id BIGINT NOT NULL REFERENCES calls(id), transcript TEXT NOT NULL CHECK(length(btrim(transcript))>0),
 transcript_sha256 TEXT NOT NULL, metadata JSONB NOT NULL,
 a_version TEXT NOT NULL, b_version TEXT NOT NULL CHECK(a_version<>b_version),
 PRIMARY KEY(comparison_id,ordinal), UNIQUE(comparison_id,call_id)
);
CREATE TABLE IF NOT EXISTS analysis_comparison_stages (
 comparison_id BIGINT NOT NULL, ordinal INTEGER NOT NULL, version TEXT NOT NULL, stage TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','received','complete','failed','uncertain')),
 response_text TEXT, result JSONB, usage JSONB, cost_units NUMERIC NOT NULL DEFAULT 0,
 error TEXT, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
 PRIMARY KEY(comparison_id,ordinal,version,stage),
 FOREIGN KEY(comparison_id,ordinal) REFERENCES analysis_comparison_calls(comparison_id,ordinal)
);
CREATE TABLE IF NOT EXISTS analysis_comparison_votes (
 comparison_id BIGINT NOT NULL, ordinal INTEGER NOT NULL, voter BIGINT NOT NULL,
 choice TEXT NOT NULL CHECK(choice IN ('a','b','equal','neither')), comment TEXT,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(comparison_id,ordinal),
 FOREIGN KEY(comparison_id,ordinal) REFERENCES analysis_comparison_calls(comparison_id,ordinal)
);
CREATE TABLE IF NOT EXISTS analysis_comparison_delivery (
 comparison_id BIGINT NOT NULL REFERENCES analysis_comparisons(id), part TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('sending','sent','failed','uncertain')),
 message_id BIGINT, error TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(comparison_id,part)
);
