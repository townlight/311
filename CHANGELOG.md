# Changelog

All notable changes to this project are documented in this file. This file is additive: historical entries below any new entry are left unchanged.

## Unreleased — 0.2.0 source work (integration candidate)

**Not released. Not tested as an installed Windows build.** This is an integration candidate only; it is not a release and makes no acceptance claim.

### Added

- Workflow library `townlight_311.workflow` under integration verification; it is an integration candidate and is not installed. The principal package is the distribution `townlight-311`, imported as `townlight_311`.
- Resident intake with privacy-aware tracking and a recovery flow.
- Staff lifecycle with existing native roles `local-admin`, `city-staff`, and `clerk`.
- Records/Data receiving requires additional permissions; Clerk gets 311 only and does not automatically receive access.
- Attachments up to 10 MiB.
- Records ZIP output reporting actual bytes plus operation history.
- SMTP notifications with administrator-managed STARTTLS settings and states `sent`, `pending`, `failed`, and `uncertain`. Delivery is not guaranteed.
- Authenticated Open311 catalogue/read/create with JSON/XML for the supported subset only (not full Open311 conformance).
- CSV/JSON import up to 2 MiB and 1000 rows, with stable external IDs, source key, and exact preview hash.
- Export capped at 10,000 requests, which fails instead of truncating when the limit would be exceeded.

### Changed

- Root metadata of the developer-only prototype helper API now points to the implemented Windows routes `/desktop/v1/service-requests`, with Open311 at `/desktop/v1/service-requests/open311/...`, and stale post-v0.2.0 roadmap and CivicAccess/CivicCode branding references were removed. Helper routes are unchanged.

### Notes

- The old `townlight_311.main` helper API remains developer-only. It is not the Windows runtime and is not a complete standalone product.
