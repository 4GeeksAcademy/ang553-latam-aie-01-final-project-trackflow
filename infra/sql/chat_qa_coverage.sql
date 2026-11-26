-- Additive event-kind migration for the existing disposable WS-07 database.
BEGIN;
DO $$
BEGIN
    IF current_database() <> 'trackflow_ws_qa' OR current_user <> 'trackflow_ws_qa' THEN
        RAISE EXCEPTION 'QA coverage requires the disposable WS-07 database';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'chat_events'::regclass AND conname = 'ck_chat_event_type'
          AND convalidated AND position('coverage_updated' IN pg_get_constraintdef(oid)) > 0
    ) THEN
        RETURN;
    END IF;
    ALTER TABLE chat_events
        DROP CONSTRAINT IF EXISTS chat_events_event_type_check,
        DROP CONSTRAINT IF EXISTS ck_chat_event_type;
    ALTER TABLE chat_events
        ADD CONSTRAINT ck_chat_event_type CHECK (
            event_type IN ('token_chunk','coverage_updated','generation_interrupted','generation_completed')
        );
END $$;
COMMIT;
