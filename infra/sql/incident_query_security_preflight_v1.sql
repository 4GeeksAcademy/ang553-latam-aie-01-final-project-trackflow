-- TrackFlow Incident Query & Tenant Security preflight v1
-- Read-only review artifact. Run manually before the corresponding DDL and
-- retain its result with the deployment evidence. It changes no database data.
BEGIN TRANSACTION READ ONLY;

SELECT current_database() AS database_name,
       current_user AS executing_role,
       current_setting('server_version_num')::integer AS server_version_num,
       current_schema() AS current_schema;

SELECT object_name,
       to_regclass(object_name) AS existing_relation
FROM (VALUES
    ('public.trackflow_client'),
    ('public.user_client_membership'),
    ('public.incident'),
    ('public.incident_history'),
    ('public.incident_id_seq'),
    ('public.incident_history_id_seq')
) AS expected(object_name);

SELECT routine_name
FROM information_schema.routines
WHERE routine_schema = 'public'
  AND routine_name = 'reject_incident_history_mutation';

ROLLBACK;
