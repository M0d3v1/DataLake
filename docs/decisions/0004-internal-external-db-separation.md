# 0004: Hard separation between platform metadata DB and external databases

## Status

Accepted (Milestone 1).

## Context

The platform has two categories of database interaction that must never be
allowed to blur together:

1. **Platform metadata** -- organizations, users, connections, pipelines,
   execution history. Owned and controlled by the platform.
2. **External source/destination databases** -- customer SQL Server /
   PostgreSQL instances the platform is told to read from or write to,
   using credentials scoped (ideally least-privilege) to that one
   integration.

If both went through the same mechanism (Django's ORM and `DATABASES`),
it becomes easy to accidentally run platform migrations against a
customer database, or to let ORM-level assumptions (connection pooling
lifetime, schema introspection, admin access) leak into how external
systems are touched.

## Decision

- Django's `DATABASES` setting contains exactly one entry: the platform
  metadata Postgres, used only via the Django ORM.
- External database connections are never registered there. Destination
  connectors (`apps.connectors.destinations`) reach them exclusively
  through SQLAlchemy Core: a `sqlalchemy.engine.Engine` built at
  operation time from `config` (host/port/database, non-secret) and
  `credential` (resolved from `apps.secrets` immediately before use),
  used for the duration of one `test_connection`/`load` call, then
  disposed (`engine.dispose()`).
- No Django model, migration, or admin ever targets an external database.

## Consequences

- A customer's database can never be affected by a Django migration
  intended for platform metadata -- there is no code path that could even
  attempt it.
- External credentials are held in memory only as long as one connector
  operation needs them, not for the lifetime of a pooled ORM connection.
- SQLAlchemy Core (not the ORM) is used for external access, so external
  schema handling stays close to the SQL involved rather than being
  mediated by Django model definitions -- appropriate, since the platform
  doesn't own those schemas and can't assume ORM-friendly conventions.
- This is a convention enforced by code review and module boundaries, not
  by a runtime guard -- nothing currently stops a future contributor from
  adding a second entry to `DATABASES`. `docs/architecture.md` calls this
  out explicitly so it stays a deliberate decision, not an accident.
