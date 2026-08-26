-- One-time grant for a DigitalOcean Managed Postgres cluster.
--
-- Postgres 15 removed the implicit CREATE privilege that every role used to
-- hold on the `public` schema. On a managed cluster the database is owned by
-- the provider's admin role (`doadmin` on DigitalOcean), so an application
-- user created afterwards can connect, and read, and do nothing else: the
-- first thing Alembic tries is CREATE TABLE alembic_version, and the cluster
-- answers "permission denied for schema public".
--
-- Nothing about that error mentions ownership or Postgres 15, and a container
-- that migrates at startup simply exits — so run this once, as the admin
-- role, against the application's own database, before the first deploy:
--
--   psql "$ADMIN_DATABASE_URL" -f deploy/grant-schema.sql
--
-- where ADMIN_DATABASE_URL is the doadmin connection string with the path
-- pointing at the application database (not `defaultdb`).

\set app_user dailyfive
\set app_db   dailyfive

GRANT ALL PRIVILEGES ON DATABASE :app_db TO :app_user;
GRANT ALL ON SCHEMA public TO :app_user;

-- Ownership rather than a bare grant, so every future migration — new tables,
-- new indexes, a dropped column — needs no further privilege work.
ALTER SCHEMA public OWNER TO :app_user;
