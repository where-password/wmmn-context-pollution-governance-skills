# Security Policy

Do not open a public Issue containing a secret or private data. Report security-sensitive problems privately to the repository owner.

The supported security boundary is a single machine and a single local user. The project does not implement IAM, OAuth, multi-tenant authorization, or remote agent identity.

Before publishing a release, maintainers must run a secret scan over the complete Git history, verify release SHA-256 sidecars, and confirm that ZIPs contain no tests, caches, databases, `.env`, `.secrets`, or production configuration.
