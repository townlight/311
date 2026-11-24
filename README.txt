# Townlight 311

Townlight 311 is the Townlight module for resident service-request intake, privacy-aware tracking, staff workflow, records, notifications, and Open311-compatible interchange.

## Status: 0.2.0 source work — integration candidate only

This repository contains **0.2.0 source work**. It is an **integration candidate**: it has **not been released**, and it has **not been tested as an installed Windows build**. Nothing here should be read as a shipped, supported, or acceptance-tested product, and no release date or release outcome is promised.

The principal package is the distribution **`townlight-311`**, imported as **`townlight_311`**.

## Workflow library under integration verification

`townlight_311.workflow` is under integration verification; it is an integration candidate and is not installed. The implemented Windows routes are `/desktop/v1/service-requests`, with Open311 at `/desktop/v1/service-requests/open311/...`.

## Resident intake, privacy, tracking, and recovery

- Residents submit service requests through resident intake.
- Tracking is privacy-aware: resident-facing tracking does not expose internal detail.
- A recovery flow lets a resident recover access to their tracking information without staff disclosing another resident's data.

## Staff lifecycle and roles

The workflow library covers the staff lifecycle and enforces the existing native roles:

- `local-admin`
- `city-staff`
- `clerk`

## Records and receiving-role permission

Records/Data receiving is gated by additional required permissions; Clerk gets 311 only and does not automatically receive Records/Data access. Records output is produced as ZIP archives that report the **actual bytes** written together with the operation history.

## Attachments

Attachments are accepted up to **10 MiB** each.

## Notifications

Notification delivery uses SMTP with STARTTLS; connection settings are managed by an administrator. Notification states are:

- `sent`
- `pending`
- `failed`
- `uncertain`

Delivery is **not guaranteed**. In particular, an SMTP acceptance is not proof that a message reached a recipient, and the `uncertain` state exists precisely because delivery status may be unknown after a send.

## Open311

The Open311 integration supports authenticated catalogue, read, and create operations with JSON/XML for the **supported subset** only. This is **not full Open311 conformance** and must not be represented as such.

## CSV/JSON import and export

- Import accepts CSV or JSON up to **2 MiB** and **1000 rows**.
- Import keys rows by **stable external IDs** together with a **source key**, and produces an **exact preview hash**.
- Export is limited to **10,000 requests**. A request that would exceed the limit **fails** instead of silently truncating the export.

## Developer-only prototype helper API

The old `townlight_311.main` FastAPI module is a **developer-only prototype helper API**. It is **not the Windows runtime** and is **not a complete standalone product**. Its existing helper routes are unchanged; do not confuse it with, or substitute it for, the implemented Windows routes (`/desktop/v1/service-requests`, with Open311 at `/desktop/v1/service-requests/open311/...`).

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

These commands describe the source tree only. They do not imply that an installed Windows build has been produced or tested.

## License

Code: AGPL-3.0-only, with the TownLight Public Institution Exception for qualifying deployments. See [LICENSE](LICENSE) and [LICENSE-EXCEPTION.md](LICENSE-EXCEPTION.md). Documentation is CC BY 4.0.
