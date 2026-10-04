-- F0.2 PostgreSQL acceptance suite.
-- Runner must SET search_path to the isolated EQS test schema before executing.
DO $$
DECLARE
  v_schema text := current_schema();
  v_tables integer;
  v_enums integer;
  v_dataset uuid;
  v_strategy uuid := '10000000-0000-0000-0000-000000000001';
  v_risk uuid;
  v_order uuid := '20000000-0000-0000-0000-000000000001';
BEGIN
  SELECT count(*) INTO v_tables
  FROM pg_tables WHERE schemaname = v_schema;
  IF v_tables <> 16 THEN
    RAISE EXCEPTION 'expected 16 core tables, found % in %', v_tables, v_schema;
  END IF;

  SELECT count(*) INTO v_enums
  FROM pg_type t
  JOIN pg_namespace n ON n.oid = t.typnamespace
  WHERE n.nspname = v_schema
    AND t.typtype = 'e'
    AND t.typname IN ('strategy_state','experiment_state','deployment_stage','risk_action');
  IF v_enums <> 4 THEN
    RAISE EXCEPTION 'expected 4 core enums, found %', v_enums;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname=v_schema AND indexname='idx_observation_available'
  ) THEN RAISE EXCEPTION 'missing idx_observation_available'; END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname=v_schema AND indexname='idx_audit_entity'
  ) THEN RAISE EXCEPTION 'missing idx_audit_entity'; END IF;

  INSERT INTO datasets(name, source, schema_version)
  VALUES ('fixture','unit-test','1') RETURNING dataset_id INTO v_dataset;
  IF v_dataset IS NULL THEN RAISE EXCEPTION 'dataset UUID default failed'; END IF;

  INSERT INTO dataset_observations_meta(
    dataset_id,instrument,event_time,published_at,available_at,received_at,revision,confidence
  ) VALUES (
    v_dataset,'TEST','2026-01-01T00:00:00Z','2026-01-01T00:00:01Z',
    '2026-01-01T00:00:02Z','2026-01-01T00:00:03Z',0,0.9
  );

  BEGIN
    INSERT INTO dataset_observations_meta(
      dataset_id,instrument,event_time,available_at,received_at,revision
    ) VALUES (v_dataset,'NEG','2026-01-01Z','2026-01-01Z','2026-01-01Z',-1);
    RAISE EXCEPTION 'negative revision was accepted';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  BEGIN
    INSERT INTO dataset_observations_meta(
      dataset_id,instrument,event_time,available_at,received_at,confidence
    ) VALUES (v_dataset,'CONF','2026-01-02Z','2026-01-02Z','2026-01-02Z',1.1);
    RAISE EXCEPTION 'confidence > 1 was accepted';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  BEGIN
    INSERT INTO dataset_observations_meta(
      dataset_id,instrument,event_time,published_at,available_at,received_at,revision,confidence
    ) VALUES (
      v_dataset,'TEST','2026-01-01T00:00:00Z','2026-01-01T00:00:01Z',
      '2026-01-01T00:00:02Z','2026-01-01T00:00:03Z',0,0.9
    );
    RAISE EXCEPTION 'duplicate dataset revision was accepted';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;

  INSERT INTO strategies(
    strategy_id,name,hypothesis,rationale,asset_universe,timeframe,data_sources,features,
    entry_rules,exit_rules,position_sizing,code_version
  ) VALUES (
    v_strategy,'fixture','falsifiable hypothesis','test rationale','["TEST"]','1m',
    '["fixture"]','[]','enter','exit','fixed','test-sha'
  );

  INSERT INTO risk_decisions(strategy_id,order_id,action,reason_codes,snapshot,policy_version)
  VALUES (v_strategy,v_order,'ALLOW','[]','{}','fixture-v1') RETURNING risk_decision_id INTO v_risk;

  INSERT INTO orders(
    order_id,strategy_id,venue,instrument,side,order_type,quantity,requested_at,
    client_order_key,status,risk_decision_id
  ) VALUES (
    v_order,v_strategy,'TESTVENUE','TEST','BUY','MARKET',1,'2026-01-01Z',
    'fixture-order-1','NEW',v_risk
  );

  INSERT INTO fills(fill_id,order_id,venue_fill_id,fill_time,quantity,price,fee)
  VALUES ('30000000-0000-0000-0000-000000000001',v_order,'venue-fill-1','2026-01-01T00:00:01Z',1,100,0.01);

  BEGIN
    INSERT INTO orders(
      order_id,strategy_id,venue,instrument,side,order_type,quantity,requested_at,
      client_order_key,status
    ) VALUES (
      '20000000-0000-0000-0000-000000000002',v_strategy,'TESTVENUE','TEST','BUY','MARKET',1,
      '2026-01-01Z','fixture-order-1','NEW'
    );
    RAISE EXCEPTION 'duplicate client_order_key was accepted';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;

  BEGIN
    INSERT INTO fills(fill_id,order_id,venue_fill_id,fill_time,quantity,price)
    VALUES (
      '30000000-0000-0000-0000-000000000002','29999999-0000-0000-0000-000000000099',
      'bad-fk','2026-01-01Z',1,100
    );
    RAISE EXCEPTION 'fill without parent order was accepted';
  EXCEPTION WHEN foreign_key_violation THEN NULL;
  END;

  INSERT INTO component_tracker(component_id,name,status,acceptance_criteria)
  VALUES ('fixture','fixture','TESTING','{}');

  BEGIN
    INSERT INTO component_tracker(component_id,name,status,acceptance_criteria)
    VALUES ('bad-status','bad-status','GREEN','{}');
    RAISE EXCEPTION 'invalid tracker status was accepted';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  INSERT INTO audit_events(actor,event_type,entity_type,entity_id,payload,event_hash)
  VALUES ('db-test','ACCEPTANCE','schema',v_schema,'{}','fixture-hash');
END $$;

SELECT
  current_schema() AS schema_name,
  (SELECT count(*) FROM pg_tables WHERE schemaname=current_schema()) AS table_count,
  (SELECT count(*) FROM datasets) AS dataset_rows,
  (SELECT count(*) FROM strategies) AS strategy_rows,
  (SELECT count(*) FROM orders) AS order_rows,
  (SELECT count(*) FROM fills) AS fill_rows,
  (SELECT count(*) FROM audit_events) AS audit_rows;
