# Changelog

All notable changes to NodeCraft. The version lives in `src/version.py`
(single source of truth). Bump it and add an entry here on every release:
PATCH = fixes, MINOR = new feature, MAJOR = breaking change.

## [1.5.0] — 2026-08-01

### Added
- **Per-feature licensing.** A single license key can now unlock any subset of
  the four modules — **Integration**, **Terminal**, **CDD Audit**, and **TRFS** —
  instead of granting everything. The enabled modules are stored in a new
  `features` field inside the signed payload, so they cannot be tampered with.
  - `license_manager.py`: new `ALL_FEATURES`, `get_enabled_features(payload)`
    and `has_feature(payload, feature)` helpers. **Backward compatible** — keys
    issued before this change (no `features` field) still unlock everything, and
    `features: "all"`/`"*"` is treated as a full license.
  - `gui/form_page.py`: the Integration, Terminal, CDD Audit and TRFS buttons
    are disabled (with an explanatory tooltip) when the active license does not
    include that feature; the click handlers also guard against it.
  - **License generator web app**: the single-key form gained per-feature
    checkboxes, and the bulk template gained an optional **Features** column
    (accepts `all`, blank, or a list like `integration, audit`). Keys are now
    signed directly in the web app so the feature list is embedded without
    touching the private keygen module. Generated Excel/results now show a
    **Features** column.

## [1.4.0] — 2026-07-30

### Added
- **Generate Scripts now emits three formats** from the same Mismatch rows
  (ported from enp-generator): the moshell **`.mos`** (unchanged, the only one
  the in-app Run button executes) plus separate ENM **CMEdit** CLI
  (`cmedit set <FDN> <param>=<value>`) and **CM Bulk** CLI
  (`set` / `FDN : …` / `param : value`) `.txt` files. The full FDN is built as
  `[<Subnetwork>,]MeContext=<node>,ManagedElement=<node>,<mo>` (Subnetwork taken
  from the main form when present). cmedit/cmbulk are review-and-apply only.
  GSM `GeranCell` MOs live on the BSC, so they use a configurable
  `gsm_fdn_prefix` template in `audit_map.json`
  (`SubNetwork=…,MeContext={bsc},ManagedElement={bsc},BscFunction=1,BscM=1,`
  `GeranCellM=1`) with `{bsc}` filled from the form's BSC name. GSM child-MO
  attributes (IdleModeAndPaging, PowerControlUplink/Downlink, Mobility, Dtm, …)
  append their child segment (`…,GeranCell=<id>,<Child>=1`), recovered from the
  audit map via `build_gsm_child_map`; cmbulk groups by full FDN so each child
  MO gets its own block; ChannelGroup keeps its real per-index instance.
- **Set values are re-formatted to the node's convention** (value from the CDD,
  format from the node): list `0 1 2 3` / `4&12&17` → `[0, 1, 2, 3]` /
  `[4, 12, 17]`; DMS `8°3'35.1"N` → `N08-03-35.1`; LTE/NR decimal degrees →
  integer µ-degrees; and a boolean `1`/`0` → the node's `ACTIVE`/`INACTIVE`
  (ON/OFF, …) domain. The compare now also treats CDD `1` == node `ACTIVE`
  (and `0` == `INACTIVE`/`OFF`/…) as a Match instead of a false mismatch.
- **LLD audit** (new optional workbook, `LLD (Tx_CPRI_Antenna)`): audits the
  physical baseband/CPRI layout from the node dump, keyed off the node-name
  suffix (`…B0<k>` ⇒ LLD `BBID` `BB<k>`; every node's baseband FRU is the local
  `BB-1`).
  - **Baseband type** — `Tx connectivity!BB-<k>` (RP6655 / BB6621) vs the node
    `FieldReplaceableUnit=BB-1` `productData.productName` (`RAN Processor 6655` /
    `Baseband 6621`), compared on the product number (`bbtype` normalizer).
  - **RiLink / CPRI port allocation** — `CPRI connectivity` rows with
    `BBID==BB<k>` vs the node `RiLink` MOs, matched by the baseband `RiPort`
    (riPortRef1): link present, `riPortRef2` → planned `Radio DATA Port`, a
    heuristic radio-type check against the radio FRU name on riPortRef2, and
    **unplanned** node links (present on the baseband but absent from the LLD)
    flagged as `Extra`.
  - LLD checks are written to their **own styled `LLD` sheet** (layout columns
    Node / BBID / Sector-Radio / Check / BB Port / Radio / Expected / Actual /
    Status), with an `LLD compliance` block in the Summary — kept out of the
    logical CDD Detail sheet and out of the moshell `set` script generator.
- cmdump parser now flattens single-level structs into dotted keys (e.g.
  `productData.productName`) so nested product data is auditable.
- `node_key_match: "prefix_of_node"` CDD-map option (site/PLA ID is a prefix of
  the node name).

## [1.3.0] — 2026-07-30

### Added
- **GSM multi-MO audit**: GeranCell parameters live across child MOs in ENM
  (IdleModeAndPaging, PowerControlUplink/Downlink, DynamicFrHrModeAdaption,
  InterRanMobility, RadioLinkTimeout, ChannelAllocAndOpt, Mobility,
  HierarchicalCellStructure, PowerControl, Dtm, MsQueuing) — each queried via
  `cmedit get *BS* GeranCell.gerancellid==<site>*,<MO>.(*) -t` and merged back
  to the cell. ~90 GSM parameters now audited (was 7), mapped from the CDD via
  the MOM/MML reference.
- **ChannelGroup (per-index)**: `CHGR_x(SDCCH/DCHNO/HSN/MAIO/HOP/NUMREQBPC)`
  audited against `GeranCell=<id>,ChannelGroup=<x>`.
- cmedit parser: configurable record key (`key_fdn`) for child/multi-instance
  MOs; attr de-dup for repeated per-index columns.
- **Batch GSM**: derive Site IDs from the batch node list (part before `_`,
  de-duplicated) and fetch GSM live per site; GSM-only batch skips dump export.
- GSM **PhysicalData** lat/long audit with a `latlong` normalizer (CDD
  `7°40'8.15"N` vs node `N07-40-08.15` compare equal as decimal degrees).
- LTE/NR **lat/long** audit: EUtranCellFDD/TDD.latitude/longitude and (via
  nRSectorCarrierRef) NRSectorCarrier.latitude/longitude, with a `geo`
  normalizer (dump integer micro-degrees 6999000 == CDD decimal 6.999).
- GSM **Trx arfcnMin/arfcnMax** audit from the RadioNode (BTS) — scoped per
  Site ID, keyed by GsmSector and matched to the CDD by CELLNAME (a separate
  node type from the BSC GeranCell tree).
- `list` normalizer for multi-value params (nccPerm/dchNo/maio/hsn): CDD
  `0 1 2 3` / `1&37` compare equal to node `[0, 1, 2, 3]` / `[1, 37]`. Raw
  values still shown; only the Status uses the normalized comparison.

- **Reverse pass for nodes missing from the CDD** (batch): a node present in the
  export/data but with no CDD row no longer vanishes from the report — its
  ACTUAL audited parameters are listed with the CDD column blank and status
  `CDD missing` (purple). LTE/NR MOs matched by ManagedElement, GSM GeranCells by
  the site's cell-id regex; the audited-attribute universe is derived from the
  map. These rows are excluded from the graded total / compliance and shown as a
  separate Summary KPI.

- **LKF install: longer wait + two-sided verify.** The status poll now runs up
  to 30 attempts (was 20), and when the job reports SKIPPED / doesn't complete,
  a node-side `alt` alarm check is the tie-breaker: if there is no *License Key
  File Fault* alarm (re-read up to 3× for a slow-finishing install), the LKF is
  treated as installed. Only rescues a false-negative — never downgrades a job
  that already reported success.

### Fixed
- **mobatch parallel Run Scripts sent the bare script path** as the mobatch
  argument, so moshell got it as a literal command (`no such command: …/$nodename
  _SetParameter_….mos`) and `$nodename` was never substituted — every node
  failed. Now passes a moshell command `'lt all;run <dir>/$nodename_SetParameter
  _<stamp>.mos'`, so mobatch substitutes `$nodename` per node and moshell runs
  that node's own script.
- **GSM cell-id prefix over-match**: a Site ID like MIN18 built the wildcard
  `gerancellid==M18*`, which also matched foreign sites (M1800.. = MIN1800,
  M18328.. = MIN1832, M1899.. = MIN189). Now filtered precisely by
  `M<siteDigits>[89]<sector-letter>` (`gsm_cell_id_re`) — applied to the GSM
  audit (drops foreign cells) AND the integration GSM Cell / relation checks
  (count only the site's own cells, not prefix false-matches).

## [1.2.0] — 2026-07-29

### Added
- **Batch / Cluster Audit**: enter a `;`-separated node list + a Cluster name to
  export all nodes live in one ENM `cmedit export` job, parse the combined dump
  (multi-node, 300 MB+ handled), and audit every node against the CDD in one run.
  Nodes are derived from the dump records (not filenames). Single-site mode
  (Site ID + auto-find / browsed dumps) still works unchanged.
- **Per-node breakdown** in the Excel Summary (Match / Mismatch / not-found +
  compliance % per node) for batch runs.
- `run_take_cm_dump_batch()` in `integration_runner` (bash `cli.py`, no AMOS).
- **Run Scripts** (edit-then-run): review/edit the generated `.mos`, pick which
  to run, confirm, then execute on the live node(s) — **sequential** (`amos`,
  one node at a time) or **parallel** via `mobatch -p <min(nodes,30)>` with the
  `$nodename` per-node script trick (one SSH command, parallelism on the gateway,
  per-node logs). Generated scripts share one timestamp and close the log with
  `l-`. After a mobatch run the per-node logs are downloaded into one folder and
  parsed (same engine as the relation logs) into a result Excel.
- **Edit Map**: in-app audit_map.json editor with live JSON validation (Save is
  blocked while invalid) — changes apply on the next Run, no restart.
- **Elapsed timer** + live **node-count** on the batch input.

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
