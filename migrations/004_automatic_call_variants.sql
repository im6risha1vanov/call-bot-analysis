-- Automatic combined reports are independent of the blind comparison experiment.
CREATE TABLE IF NOT EXISTS automatic_call_reports (
 call_id BIGINT NOT NULL REFERENCES calls(id), version TEXT NOT NULL,
 client_id INTEGER NOT NULL REFERENCES clients(id),
 transcript_sha256 TEXT NOT NULL, transcript TEXT NOT NULL, metadata JSONB NOT NULL,
 package_sha256 TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
 prompt TEXT NOT NULL, settings JSONB NOT NULL, config JSONB NOT NULL, catalog JSONB NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','received','complete','failed','uncertain')),
 response_text TEXT, result JSONB, cost_units NUMERIC NOT NULL DEFAULT 0, usage JSONB, error TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ,
 PRIMARY KEY(call_id,version,transcript_sha256)
);
CREATE INDEX IF NOT EXISTS automatic_call_reports_client_status_idx
 ON automatic_call_reports(client_id,status,created_at);
CREATE TABLE IF NOT EXISTS automatic_call_spend (
 client_id INTEGER NOT NULL REFERENCES clients(id), day DATE NOT NULL,
 spent_units NUMERIC NOT NULL DEFAULT 0, PRIMARY KEY(client_id,day)
);
