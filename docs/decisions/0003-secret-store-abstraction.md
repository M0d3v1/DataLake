# 0003: Secret storage behind an interface, encrypted-field default

## Status

Accepted (Milestone 1). The default backend is explicitly a placeholder,
not a production recommendation -- see Consequences.

## Context

Connections need credential material (API keys, tokens, DB passwords) that
must never be stored in plaintext, must never appear in logs, and ideally
should be swappable for whatever secrets infrastructure a given deployment
already runs (Vault, AWS Secrets Manager, etc.) without touching the code
that uses secrets.

## Decision

Define `apps.secrets.base.SecretStore` as an interface
(`store(plaintext) -> secret_ref`, `retrieve(secret_ref) -> plaintext`,
`delete(secret_ref)`) and make it the *only* way application code touches
credential material. `Credential.resolve()` and connector `credential`
arguments always go through this interface; no model field ever holds a
plaintext secret.

Ship one default implementation, `EncryptedFieldSecretStore`: the full
credential payload is JSON-encoded and encrypted with Fernet (symmetric
encryption) using a key from `settings.SECRET_STORE_ENCRYPTION_KEY`, then
stored as ciphertext in a tenant-scoped `EncryptedSecret` row. The backend
is selected via `settings.SECRET_STORE_BACKEND` (a dotted path).

## Consequences

- Swapping to a real secrets manager later means implementing `SecretStore`
  and changing one settings value -- no changes to `Credential`, connectors,
  or auth providers.
- The default implementation has real limitations that must stay visible,
  not get quietly treated as "secure enough": a single application-level
  symmetric key, no rotation, no hardware-backed protection, no audit trail
  at the secrets-manager level (only at the `apps.auditing` level, which is
  a much weaker guarantee). It's appropriate for local development and
  small self-hosted deployments; it is *not* a substitute for a managed
  secrets service in an environment handling real carrier/customer
  credentials.
- Losing `SECRET_STORE_ENCRYPTION_KEY` makes every stored secret
  permanently unrecoverable (`SecretNotFound` on decrypt failure) -- key
  management/backup for that value is an operational responsibility this
  ADR does not solve.
