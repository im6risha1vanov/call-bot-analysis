-- Trial combined reports; preserve original blind experiment and production results.
CREATE TABLE IF NOT EXISTS combined_reports (
 comparison_id BIGINT NOT NULL, ordinal INTEGER NOT NULL, version TEXT NOT NULL,
 client_id INTEGER NOT NULL REFERENCES clients(id), initiator BIGINT NOT NULL,
 package_sha256 TEXT NOT NULL, transcript_sha256 TEXT NOT NULL,
 prompt TEXT NOT NULL, settings JSONB NOT NULL, config JSONB NOT NULL, catalog JSONB NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','received','complete','failed','uncertain')),
 response_text TEXT, result JSONB, cost_units NUMERIC NOT NULL DEFAULT 0,usage JSONB,error TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ,
 PRIMARY KEY(comparison_id,ordinal,version),
 FOREIGN KEY(comparison_id,ordinal) REFERENCES analysis_comparison_calls(comparison_id,ordinal)
);
CREATE TABLE IF NOT EXISTS combined_report_delivery (
 comparison_id BIGINT NOT NULL, ordinal INTEGER NOT NULL,version TEXT NOT NULL,
 recipient BIGINT NOT NULL,part INTEGER NOT NULL CHECK(part>=0),
 status TEXT NOT NULL CHECK(status IN ('sending','sent','failed','uncertain')),
 message_id BIGINT,error TEXT,updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(comparison_id,ordinal,version,recipient,part),
 FOREIGN KEY(comparison_id,ordinal,version) REFERENCES combined_reports(comparison_id,ordinal,version)
);
