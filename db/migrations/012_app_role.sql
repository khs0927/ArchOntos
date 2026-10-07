BEGIN;

-- Least-privilege application role. Migrations run as the schema owner (the migrator);
-- services connect as a LOGIN role that is a member of `archontos_app`, which is neither
-- superuser nor BYPASSRLS and owns nothing, so every row-level security policy applies to it.
-- Creating the group role needs CREATEROLE on the migrator; the LOGIN role (with its secret)
-- is provisioned outside migrations: `python -m archontos.db.roles ensure-login`.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'archontos_app') THEN
    CREATE ROLE archontos_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END
$$;

DO $$
DECLARE
  s text := current_schema();
BEGIN
  EXECUTE format('GRANT USAGE ON SCHEMA %I TO archontos_app', s);
  EXECUTE format(
    'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO archontos_app', s);
  EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO archontos_app', s);
  -- Tables added by later migrations (created by this same migrator role).
  EXECUTE format(
    'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO archontos_app', s);
  EXECUTE format(
    'ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO archontos_app', s);
  -- Defense in depth on top of the RLS policies from 011: no UPDATE/DELETE privilege at all.
  EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I.audit_log FROM archontos_app', s);
  -- The application reads migration state but never writes it.
  IF to_regclass(format('%I.schema_migrations', s)) IS NOT NULL THEN
    EXECUTE format(
      'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON %I.schema_migrations FROM archontos_app', s);
  END IF;
END
$$;

COMMIT;
