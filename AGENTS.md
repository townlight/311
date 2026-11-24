<a id="townlight-311-agent-board"></a>

# Townlight 311 Agent Board

## Source of Truth

- Upstream suite spec: `townlight/townlight/docs/CivicSuiteUnifiedSpec.md`, especially the Townlight 311 catalog entry and suite-wide non-negotiables.
- Townlight 311 supports resident service request intake, deterministic triage, duplicate-candidate review, department routing, durable resident/staff workflow, records, notifications, and Open311-compatible interchange checklists.
- Staff own every decision.
- The principal package is the distribution `townlight-311`, imported as `townlight_311`.

## Hard Boundaries

- Townlight 311 never dispatches crews, creates official work orders, handles emergencies, provides legal advice, writes back to 311/work-order systems, or updates a 311 system of record.
- The owner's explicit `0.2.0` plan supersedes the `0.1.1` prototype non-persistence constraint. The current native `townlight_311` module implements durable resident/staff workflow; that persistence does not authorize emergency, dispatch, or external vendor write-backs.
- Townlight 311 must not call live LLMs or live external vendor/311/work-order systems.
- Triage, duplicate-candidate review, routing, and exports must be marked staff-review-required where applicable.
- Townlight 311 depends on Townlight Core; Townlight Core must never depend on Townlight 311.
- Townlight 311 may reference Townlight Access and Townlight Code concepts only through released APIs or deterministic sample data.
- Roles are `local-admin`, `city-staff`, and `clerk`; no new roles are introduced.

## Verification

Run `bash scripts/verify-release.sh` before every push or release.
