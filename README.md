# Townlight 311

Townlight 311 is the Townlight module for resident service-request intake, privacy-aware tracking, staff workflow, records, notifications, and Open311-compatible interchange.

## Status: 0.2.0 ships in the Windows 1.10.0.1 public beta

Townlight 311 0.2.0 is included in the signed [Townlight for Windows 1.10.0.1](https://github.com/townlight/townlight/releases/tag/townlight-windows-v1.10.0.1) public beta. In the [release acceptance record](https://github.com/townlight/townlight/releases/download/townlight-windows-v1.10.0.1/ACCEPTANCE-Windows-1.10.0.1.md), on two Windows PCs, a resident submitted a 311 request with a photo, followed its status and saw the public comment. The staff workflow completed eight observed milestones across 311, Communications and Parks. This is a public beta, not a general-availability release.

The principal package is the distribution **`townlight-311`**, imported as **`townlight_311`**.

## Workflow library

`townlight_311.workflow` is included in the Windows 1.10.0.1 public beta. The implemented Windows routes are `/desktop/v1/service-requests`, with Open311 at `/desktop/v1/service-requests/open311/...`.

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

These commands run the source tree. The installed Windows build is the signed 1.10.0.1 public beta.

## License

Code: AGPL-3.0-only, with the TownLight Public Institution Exception for qualifying deployments. See [LICENSE](LICENSE) and [LICENSE-EXCEPTION.md](LICENSE-EXCEPTION.md). Documentation is CC BY 4.0.
