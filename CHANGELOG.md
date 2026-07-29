# Changelog

All notable changes to NodeCraft. The version lives in `src/version.py`
(single source of truth). Bump it and add an entry here on every release:
PATCH = fixes, MINOR = new feature, MAJOR = breaking change.

## [1.1.0] — 2026-07-29

### Added — CDD Audit (new subsystem)
- Standalone **CDD Audit** page: enter Site ID, browse LTE / NR / GSM CDD,
  auto-find node dumps in `LOG/<SiteID>/DUMP/`, compare, export Excel.
- **Config-driven mapping** (`audit_map.json`): sheets/columns → MO attributes,
  editable without a rebuild (re-read every Run).
- **Node** audit (ManagedElement `userLabel`, ENodeBFunction params).
- **LTE cell** audit incl. `EUtranCellTDD` vs `EUtranCellFDD` auto-selection by
  Freq Band (`cell_mo_map`).
- **LTE relation** audit with band → real-cell expansion (`cell_expand`).
- **NR (NSA)** profiles: NRCellDU + NRSectorCarrier (via `nRSectorCarrierRef`).
- **GSM** audit via live `cmedit` over the prepared SSH session (bash `cli.py`,
  scoped by Site ID) — no moshell login.
- **Follow-reference** (`via_ref`): audit attributes on a referenced MO (e.g.
  `sectorCarrierRef` → SectorCarrier); the report/script point at the real MO
  with a **Reference Cell** column.
- **Value split** (`split`): one CDD column → several real params (e.g. MIMO
  `32T32R` → `noOfTxAntennas` / `noOfRxAntennas`).
- **Per-segment normalize** (`norm: "segments"`) for CGI (`00087` == `87`).
- **Browse dump(s)**: multi-file, cmdump + modump, auto-detected; filtered to
  the entered Site ID.
- **Generate Scripts**: one moshell `set` script per node from Mismatch rows
  (CDD-expected values), grouped by top-level MO and sorted by parameter.
- Styled **Summary** sheet (banner, colored KPIs, Share %, Compliance).

### Fixed
- cmedit `-t` parser (tab-separated, per-level DN) — GSM returned 0 MOs.
- cmdump parser now also reads generic 3GPP `<xn:attributes>` (e.g.
  ManagedElement `userLabel`), not only Ericsson `vsData`.
- Node-aware compare so merged multi-node dumps don't collide.
- GSM branch no longer pulls a node from the main form (leaked foreign nodes,
  e.g. a leftover MIN4893, into the report).

### Changed
- App version is now centralized in `src/version.py`; window title + exe
  metadata read from it.

## [1.0.0]
- Initial NodeCraft: RAN node integration (LTE/NR/GSM) via AMOS/ENM, SW level
  check, dumps, reporting.
