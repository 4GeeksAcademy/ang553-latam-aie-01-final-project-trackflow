BEGIN;

DO $$
DECLARE
  target_table regclass := to_regclass('rfp_authorized_business_inputs');
  existing_name text;
  existing_definition text;
  matching_count integer;
BEGIN
  IF target_table IS NULL THEN
    RAISE EXCEPTION 'rfp_authorized_business_inputs is not installed in the active schema';
  END IF;

  SELECT count(*), min(c.conname), min(pg_get_constraintdef(c.oid, true))
    INTO matching_count, existing_name, existing_definition
  FROM pg_constraint AS c
  WHERE c.conrelid = target_table
    AND c.contype = 'c'
    AND pg_get_constraintdef(c.oid, true) LIKE '%input_kind%'
    AND pg_get_constraintdef(c.oid, true) LIKE '%client_price_quote%'
    AND pg_get_constraintdef(c.oid, true) LIKE '%volume_discount%';

  IF matching_count <> 1 THEN
    RAISE EXCEPTION 'expected exactly one business input kind constraint, found %', matching_count;
  END IF;

  IF existing_definition LIKE '%monthly_storage_volume%' THEN
    IF existing_name <> 'ck_rfp_business_input_kind' THEN
      RAISE EXCEPTION 'monthly storage volume constraint has unexpected name %', existing_name;
    END IF;
    RETURN;
  END IF;

  EXECUTE format(
    'ALTER TABLE %s DROP CONSTRAINT %I', target_table, existing_name
  );
  EXECUTE format(
    'ALTER TABLE %s ADD CONSTRAINT ck_rfp_business_input_kind CHECK '
    || '(input_kind IN (''client_price_quote'',''warehouse_capacity'',''operational_scope'','
    || '''currency_quote'',''currency_conversion'',''global_commercial_term'','
    || '''volume_discount'',''monthly_storage_volume''))',
    target_table
  );
END;
$$;

COMMIT;
