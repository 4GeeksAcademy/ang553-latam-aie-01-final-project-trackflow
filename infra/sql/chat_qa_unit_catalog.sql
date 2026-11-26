-- Explicitly opt-in synthetic knowledge publication for the disposable WS-07 database.
BEGIN;
DO $$
BEGIN
    IF current_database() <> 'trackflow_ws_qa' OR current_user <> 'trackflow_ws_qa' THEN
        RAISE EXCEPTION 'The QA knowledge catalog requires the disposable WS-07 database';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS qa_unit_catalog (
    unit_id text NOT NULL,
    version text NOT NULL,
    client_id text NOT NULL REFERENCES trackflow_client(client_id),
    approved_text text NOT NULL,
    language text NOT NULL CHECK (language = 'es'),
    source_document text NOT NULL CHECK (source_document ~ '^qa-[a-z0-9-]+$'),
    source_section text NOT NULL,
    anchor_document text NOT NULL,
    anchor_section text NOT NULL,
    anchor_sha256 text NOT NULL CHECK (anchor_sha256 ~ '^[a-f0-9]{64}$'),
    topic_aliases text[] NOT NULL CHECK (cardinality(topic_aliases) > 0),
    qualifiers text[] NOT NULL CHECK (cardinality(qualifiers) > 0),
    composition_group text NOT NULL,
    status text NOT NULL CHECK (status IN ('published', 'revoked', 'draft')),
    valid_until timestamptz,
    PRIMARY KEY (unit_id, version, client_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS qa_unit_catalog_published_version
    ON qa_unit_catalog (unit_id, client_id) WHERE status = 'published';

WITH fixture(unit_id, source_section, approved_text, aliases, qualifier) AS (
    VALUES
    ('DEV-PLAZO', 'plazo',
     'En la política ficticia de QA I14, el plazo es de 21 días hábiles desde la entrega, solo para pedidos de prueba.',
     ARRAY['plazo', 'limite', 'ventana'], 'solo para pedidos de prueba'),
    ('DEV-COSTE', 'coste',
     'En la política ficticia de QA I14, la marca de prueba cubre el coste únicamente si aprueba la etiqueta de devolución.',
     ARRAY['coste', 'costo', 'costos', 'costes', 'precio', 'paga', 'pagar'], 'únicamente si aprueba la etiqueta')
)
INSERT INTO qa_unit_catalog (
    unit_id, version, client_id, approved_text, language,
    source_document, source_section, anchor_document, anchor_section,
    anchor_sha256, topic_aliases, qualifiers, composition_group, status
)
SELECT unit_id, '1.0.0', '7f6e3d21-8c4a-4b92-a1d7-5e9f2c6b1048', approved_text, 'es',
       'qa-i14-devoluciones', source_section, 'returns-policy', 'return-window-and-costs',
       '817f49fa4699ae8688c222ff3726ca0fed08e3c914a0c48d843b4a8ca6db5403',
       aliases, ARRAY[qualifier], 'qa-i14-devoluciones', 'published'
FROM fixture
ON CONFLICT DO NOTHING;
COMMIT;
