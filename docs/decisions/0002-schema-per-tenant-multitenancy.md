# 0002: Schema-per-tenant multi-tenancy

## Status

Accepted (Milestone 1).

## Context

The platform is multi-tenant at the organization level: connections,
credentials, pipelines, and execution history all belong to one
organization and must never be visible to another. Two realistic options:

1. **Single schema, org-scoped rows.** Every table carries an `org_id`
   foreign key; isolation is enforced by always filtering on it in the
   application/query layer. Simplest to build and operate; isolation is
   only as strong as the discipline of every query that touches
   tenant data.
2. **Schema-per-tenant.** Each organization gets its own PostgreSQL schema
   inside the same database instance; tenant-scoped tables exist once per
   schema, physically separated. Stronger isolation (a missing `WHERE`
   clause can't leak another tenant's rows -- there's no row to leak, it's
   in a different schema), at the cost of real infrastructure: dynamic
   schema creation/migration, and a request-time mechanism to select the
   right schema.

Given the domain (insurance/insurtech: often-regulated customer data,
credentials for third-party systems), the stronger isolation guarantee was
judged worth the added complexity, particularly since a row-scoping bug is
exactly the kind of mistake that's easy to make and expensive to have made.

## Decision

Use [django-tenants](https://django-tenants.readthedocs.io/) for
schema-per-tenant multi-tenancy on a single PostgreSQL instance:

- `SHARED_APPS` (django-tenants' own bookkeeping, `orgs` -- which holds the
  tenant/domain registry itself --, and `accounts`/Django auth, since a
  user must be resolved before we know which tenant to route into) live in
  the `public` schema, one copy.
- `TENANT_APPS` (`secrets`, `connectors`, `authproviders`, `connections`,
  `pipelines`, `execution`, `rawstore`, `auditing`) are migrated into
  *every* tenant's schema separately.
- `Organization.auto_create_schema = True`: saving a new `Organization`
  automatically creates and migrates its schema.
- `Organization.auto_drop_schema = False`, deliberately: deleting an
  `Organization` row does not silently drop its schema (and the data in
  it). Schema teardown is a separate, explicit operational action.

## Consequences

- Isolation is enforced by Postgres itself (`search_path`), not by
  remembering to filter every query -- a materially stronger guarantee for
  the parts of the schema (credentials, raw payloads, audit log) where a
  leak would matter most.
- Real added complexity: `migrate_schemas` instead of `migrate`, tests for
  tenant-scoped models must use `django_tenants.test.cases.TenantTestCase`
  (see `apps/*/tests`), and Celery tasks must explicitly enter the right
  tenant's schema (`django_tenants.utils.schema_context`) since a worker
  process serves all tenants.
- Schema count grows with organization count. For the scale this product
  targets (enterprise insurance data teams, not a high-volume
  consumer SaaS), that's an acceptable, well-trodden pattern; it would
  need revisiting well before thousands of tenants.
- Cross-schema reporting/analytics across all tenants (if ever needed) is
  harder than with row-scoping -- deliberately out of scope for now.
