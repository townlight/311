<a id="civic311-agent-board"></a>

# Townlight 311 Agent Board

## Source of Truth

- Upstream suite spec: `townlight/townlight/docs/CivicSuiteUnifiedSpec.md`, especially the Townlight 311 catalog entry and suite-wide non-negotiables.
- Townlight 311 supports resident service request intake, deterministic triage, duplicate-candidate review, department routing, and Open311-compatible export checklists.
- Staff own every decision.

## Hard Boundaries

- Townlight 311 never dispatches crews, creates official work orders, handles emergencies, provides legal advice, writes back to 311/work-order systems, or updates a 311 system of record.
- Townlight 311 v0.1.1 must not call live LLMs or live 311/work-order systems.
- Triage, duplicate-candidate review, routing, and exports must be marked staff-review-required where applicable.
- Townlight 311 depends on Townlight Core; Townlight Core must never depend on Townlight 311.
- Townlight 311 may reference Townlight Access and Townlight Code concepts only through released APIs or deterministic sample data in v0.1.1.

## Verification

Run `bash scripts/verify-release.sh` before every push or release.
