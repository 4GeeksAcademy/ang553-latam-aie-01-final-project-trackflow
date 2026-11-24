BEGIN;

DO $$
DECLARE
  target_table regclass := to_regclass('rfp_approval_validation_bundles');
  constraint_oid oid;
  constraint_marker text;
  constraint_definition text;
BEGIN
  IF target_table IS NULL THEN
    RAISE EXCEPTION
      'rfp_approval_validation_bundles is not installed in the active schema';
  END IF;

  SELECT c.oid, obj_description(c.oid, 'pg_constraint'),
         pg_get_constraintdef(c.oid, true)
    INTO constraint_oid, constraint_marker, constraint_definition
  FROM pg_constraint AS c
  WHERE c.conrelid = target_table
    AND c.conname = 'ck_rfp_validation_bundle_eligibility'
    AND c.contype = 'c';

  IF constraint_marker = 'trackflow:indicative-validation-bundle-v1' THEN
    IF constraint_definition NOT LIKE '%MONTHLY_STORAGE_VOLUME_REQUIRED%'
       OR constraint_definition NOT LIKE '%LAST_MILE_RATE_REQUIRED%'
       OR constraint_definition NOT LIKE '%RETURN_PROCESSING_RATE_REQUIRED%'
       OR constraint_definition NOT LIKE '%VOLUME_DISCOUNT_APPROVAL_REQUIRED%'
       OR constraint_definition NOT LIKE '%blocks_commercial_completeness =%'
    THEN
      RAISE EXCEPTION
        'indicative validation bundle constraint marker disagrees with definition';
    END IF;
    RETURN;
  END IF;

  IF constraint_oid IS NULL THEN
    RAISE EXCEPTION
      'ck_rfp_validation_bundle_eligibility is missing from the active schema';
  END IF;

  ALTER TABLE rfp_approval_validation_bundles
    DROP CONSTRAINT ck_rfp_validation_bundle_eligibility;

  ALTER TABLE rfp_approval_validation_bundles
    ADD CONSTRAINT ck_rfp_validation_bundle_eligibility CHECK(
      eligible=(
        (scope_key<>'proposal' OR source_departments_approved)
        AND mandatory_compliance_passed
        AND jsonb_array_length(blocking_rule_ids)=0
        AND jsonb_array_length(open_conflict_ids)=0
        AND (
          (
            NOT blocks_commercial_completeness
            AND jsonb_array_length(unresolved_input_codes)=0
          )
          OR (
            blocks_commercial_completeness
            AND jsonb_array_length(unresolved_input_codes)>0
            AND unresolved_input_codes <@ '[
              "MONTHLY_STORAGE_VOLUME_REQUIRED",
              "LAST_MILE_RATE_REQUIRED",
              "RETURN_PROCESSING_RATE_REQUIRED",
              "VOLUME_DISCOUNT_APPROVAL_REQUIRED"
            ]'::jsonb
            AND jsonb_array_length(unresolved_input_codes)=(
              CASE WHEN unresolved_input_codes ?
                'MONTHLY_STORAGE_VOLUME_REQUIRED' THEN 1 ELSE 0 END
              + CASE WHEN unresolved_input_codes ?
                'LAST_MILE_RATE_REQUIRED' THEN 1 ELSE 0 END
              + CASE WHEN unresolved_input_codes ?
                'RETURN_PROCESSING_RATE_REQUIRED' THEN 1 ELSE 0 END
              + CASE WHEN unresolved_input_codes ?
                'VOLUME_DISCOUNT_APPROVAL_REQUIRED' THEN 1 ELSE 0 END
            )
          )
        )
      )
      AND blocks_commercial_completeness=(
        jsonb_array_length(unresolved_input_codes)>0
      )
    );

  COMMENT ON CONSTRAINT ck_rfp_validation_bundle_eligibility
    ON rfp_approval_validation_bundles IS
    'trackflow:indicative-validation-bundle-v1';
END;
$$;

COMMIT;
