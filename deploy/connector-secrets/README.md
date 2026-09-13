# Local connector credentials

Do not commit credential files. For tenant `TENANT_ID` and the connector's opaque
`credential_ref`, create this read-only path:

```text
deploy/connector-secrets/<sha256(TENANT_ID)>/<credential_ref>
```

The worker derives the tenant directory itself, refuses symlinks and non-regular files,
and never returns the credential in connector evidence. This directory is only the local
Compose/BYOC adapter. Managed GCP uses deterministic Secret Manager names and per-secret
IAM instead.
