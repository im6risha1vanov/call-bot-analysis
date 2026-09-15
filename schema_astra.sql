-- Расширение общей Postgres-базы (той же, что использует /opt/callbot) для
-- параллельного разбора звонков через Astra — сравнение с Claude на тех же
-- реальных звонках. Отдельная таблица, а не колонки на calls: не трогаем
-- схему, которой владеет Claude-сторона, и удобно дропнуть/пересоздать
-- независимо при экспериментах с промтами.

CREATE TABLE IF NOT EXISTS astra_analysis (
    call_id                 BIGINT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
    status                  TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'processing', 'analyzed', 'failed')),
    attempts                INTEGER NOT NULL DEFAULT 0,
    transcript              TEXT,           -- отдельная копия не хранится: используем calls.transcript
    analysis                JSONB,          -- scores + rows (аналог calls.analysis у Claude)
    score                   INTEGER,
    level                   TEXT,
    short_report            JSONB,
    detailed_report         JSONB,
    detailed_report_requested_at TIMESTAMPTZ,
    cost_units               NUMERIC(14,4), -- "кредит-токены" Astra + Deepgram-доля не считаем повторно (транскрипт переиспользуется)
    immediate_sent_manager  BOOLEAN NOT NULL DEFAULT false,
    immediate_sent_head     BOOLEAN NOT NULL DEFAULT false,
    digest_included         BOOLEAN NOT NULL DEFAULT false,
    error                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS astra_analysis_status_idx ON astra_analysis (status);

-- Дайджест Astra шлётся независимо от дайджеста Claude (тот же клиент,
-- отдельный бот) — не переиспользуем clients.last_digest_sent_date, чтобы не
-- конфликтовать с гейтингом Claude-воркера.
CREATE TABLE IF NOT EXISTS astra_digest_state (
    client_id            INTEGER PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
    last_digest_sent_date DATE
);

-- Отдельный от Claude-стороны учёт расхода: там daily_spend в рублях по
-- реальному прайсу Anthropic, здесь — в условных "кредит-единицах" Astra,
-- смешивать валюты в одну таблицу было бы нечестно (см. сравнение цены).
CREATE TABLE IF NOT EXISTS astra_daily_spend (
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    spent_units NUMERIC(16,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (client_id, day)
);
