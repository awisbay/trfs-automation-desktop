# Changelog

All notable changes to NodeCraft. The version lives in `src/version.py`
(single source of truth). Bump it and add an entry here on every release:
PATCH = fixes, MINOR = new feature, MAJOR = breaking change.

## [1.9.10] - 2026-08-12

### Changed
- **LLD radio check now double-checks the Sector.** The radio HW match no longer
  passes on band alone — it also requires the radio FRU's RRU index (the digits
  after `RRU`) to line up with the planned `Sector`. So a shared radio named
  `B0B28_RRU123_1` on Sector 1 is now a Mismatch (RRU should be per-sector,
  `RRU1`/`RRU2`/`RRU3`); when the LLD itself plans a shared `Sector=123` and the
  node matches, it stays Match.

## [1.9.9] - 2026-08-12

### Added
- **License anti-rollback (system-clock backdate protection).** The app keeps a
  small HMAC-protected, hostname-bound "last seen" timestamp that only moves
  forward; if the clock is later found set back (beyond a 1-day grace for
  TZ/DST jitter) the license is refused with a clear message. Closes the easy
  expiry bypass of setting the PC date back. Runs once at startup — no
  measurable performance cost. (A binary-patching attacker remains out of scope
  for client-side licensing.)

## [1.9.8] - 2026-08-11

### Changed
- **"Extra" status renamed to "Unplanned" and recoloured orange.** The
  node-only (not-in-CDD) status in the Cell Inventory and LLD sheets — plus the
  Summary tally and log lines — now reads "Unplanned" with an orange fill
  instead of "Extra" in blue.

## [1.9.7] - 2026-08-11

### Fixed
- **GSM-only offline audit is valid without any dump.** When there is no dump,
  no GSM cmedit log and no ENM login, the audit no longer fails with "No node
  data" as long as a Site ID and a CDD are given — it proceeds and reports the
  CDD params as NotFound for the site (same as a live audit that finds no cell).
  Source priority is unchanged: uploaded log → live (ENM login) → skip/NotFound.

## [1.9.6] - 2026-08-11

### Fixed
- **ElecTilt `digitalTilt` fallback restricted to AIR/AAS sectors.** The
  SectorCarrier.digitalTilt fallback now applies only when the sector's radio is
  an AIR/AAS unit (resolved via `sectorFunctionRef` →
  `SectorEquipmentFunction.rfBranchRef` → radio `productName`, e.g. `AIR 3265`).
  A plain RET-fed sector that fails to match a RET is left NotFound instead of
  silently borrowing digitalTilt.

## [1.9.5] - 2026-08-11

### Fixed
- **ElecTilt fallback for LTE AIR/AAS sectors.** A sector served by an AIR
  antenna with no physical RET (e.g. a 3-sector site whose RETs only cover
  sectors 1–2) was reported NotFound. The tilt now falls back to the cell's
  `SectorCarrier.digitalTilt` (via `sectorCarrierRef`) for LTE — mirroring the
  NR `CommonBeamforming.digitalTilt` fallback — so those cells resolve. RET
  stays the primary source when present.

## [1.9.4] - 2026-08-11

### Added
- **ElecTilt mismatches generate a `.mos` fix.** A tilt Mismatch now emits a
  moshell `set <RetSubUnit> electricalAntennaTilt <deg×10>` line (one per
  physical RET, de-duped), converting the CDD degrees to the node's 0.1° units.
  Tilt is a RET operation, so it goes into the `.mos` only — cmedit/cmbulk skip
  it. The `.mos` generator now applies node-convention value formatting
  (list/DMS/geo/boolean/tilt) like the cmedit/cmbulk exports already did.

## [1.9.3] - 2026-08-11

### Added
- **Electrical antenna tilt (ElecTilt) audit.** Each cell's CDD `ElecTilt` is
  checked against the node's antenna tilt. With no direct cell→RET reference in
  the dump, cells are linked to their `RetSubUnit` by the RET `userLabel`
  convention (`<site><band-letters>-<sector>`, e.g. `GFATIMLY-1` for band L /
  sector 1) and compared to `electricalAntennaTilt` (0.1° → CDD degrees, node 20
  == CDD 2). NR / AIR-beamforming cells with no physical RET fall back to
  `CommonBeamforming.digitalTilt`. Reported per cell in the Detail sheet.

## [1.9.2] - 2026-08-11

### Added
- **GsmSector in the Cell Inventory.** The Cell Inventory sheet now lists
  `GsmSector` (from the CDD `CELLNAME` column vs the node's GsmSector MOs),
  alongside EUtranCell/NRCell/GeranCell — so a missing or extra GSM sector shows
  up per-cell, not just a count.

## [1.9.1] - 2026-08-11

### Fixed
- **modump: cell → SectorCarrier/NRSectorCarrier reference now parsed.** The DCG
  parser dropped list-reference attributes rendered as `sectorCarrierRef[1]`
  followed by `>>> sectorCarrierRef = <FDN>`. It now captures them, so every
  via-ref param resolves from a modump (was NotFound): configuredMaxTxPower,
  sectorCarrierType, noOf(Used)Tx/RxAntennas, arfcnDL/UL, bSChannelBwDL,
  latitude, longitude.
- **Baseband HW type from modump.** `FieldReplaceableUnit=BB-1` stores its
  identity in a `productData` struct (`{…, productName=RAN Processor 6655, …}`)
  rather than a standalone `productName` — the LLD audit now parses it, so the
  baseband row matches instead of reading empty.
- **EUtranCellTDD bandwidth.** DL BW falls back to `channelBandwidth` for TDD
  cells (which have no dl/ulChannelBandwidth), so it no longer reads NotFound.
- **Null attribute is a Mismatch, not NotFound.** When the MO exists but the
  audited attribute is absent (the node's value is null/unset) it is now a
  Mismatch (`actual = "(not set)"`); NotFound/MO_NotFound stay for a value that
  cannot be located or a missing MO.
- **Enum values compare on their label.** A dump enum `0 (NORMAL_SECTOR)` now
  matches the CDD's `NORMAL_SECTOR`.

## [1.9.0] - 2026-08-06

### Added
- **ESS (LTE/NR spectrum sharing) audit.** New "ESS" sheet in the report. For
  each pair on the CDD's `ESS` sheet it checks, against the node dump: the LTE
  and NR cells exist; `essScLocalId`/`essScPairId` are equal on the LTE
  `SectorCarrier` and NR `NRSectorCarrier` (and match the CDD); and
  `essEnabled=true` on both the LTE `GUtranCellRelation` and NR
  `EUtranCellRelation` (matched by gNB ID so a neighbour relation can't clobber
  the pair).
- **DL BW audit** — new `bw` normalizer compares the CDD's MHz against the
  node's kHz `dlChannelBandwidth`/`ulChannelBandwidth` (10 == 10000).
- **MIMO fallback** — new `attr_alt`: the antenna count matches against
  `noOfTxAntennas` *or* `noOfUsedTxAntennas` (and Rx likewise), so a `0` primary
  with a valid "used" value still validates (and the used value is displayed).

### Changed
- **Trimmed the audit parameter set.** Per request, ~136 LTE/NR/GSM parameters
  were removed from `audit_map.json` (and 15 now-empty GSM/LTE profiles dropped)
  so the audit focuses on the parameters that matter for this scope.
- **GSM audit improvements.**
  - TRX count now audits the CDD `No of UL TRX` column directly against the
    number of `Trx` per `GsmSector` (simpler than the old `TRX COUNT` formula).
  - Added GSM node `UserLabel_Node` audit (`ManagedElement.userLabel`, like LTE).
  - Added GSM cell `gsmSectorId` audit from the CDD `CELLNAME` column.
  - BSC IP-broker now reports EVERY `AbisIp` MO (one row per `GsmSector`),
    not a single aggregated row — so a single wrongly-brokered sector shows.
  - CDD header matching is whitespace-tolerant (double spaces / nbsp), e.g.
    `No of  UL TRX` matches `No of UL TRX`.

### Added (continued)
- **Offline GSM (GeranCell) audit via an uploaded cmedit log.** The CDD Audit
  page has a new "GSM cmedit log (txt)" field. When set, the GSM BSC audit is
  driven by that log instead of a live SSH cmedit — same records, same audit.
  Leave it blank to keep the live path. The log is produced by a **separate**
  standalone ENM script, `tools/gsm_cmedit_dump.py` (not bundled in the app):
  the operator runs it on the ENM scripting host, enters a SITE ID (e.g.
  MIN340), and it collects every GeranCell (+ child MO) parameter scoped to that
  site with band-anchored `M<digits>8*/M<digits>9*` prefixes so neighbours like
  MIN3407 / MIN3405 are excluded. The upload parser applies the same precise
  site filter as a safety net.

## [1.8.4] - 2026-08-06

### Added
- **Remark column in the Integration Summary.** The saved summary Excel (and the
  clipboard copy) now has a trailing "Remark" column next to the Yes/No/Pending
  status, filled with each step's short progress detail — e.g. the SGw check's
  "1/29 with packet loss". With more than one node the remark is prefixed by the
  node label. The column is left-aligned and text-wrapped.

## [1.8.3] - 2026-08-06

### Changed
- **Apply (cmedit) is now per-node and honors edited scripts.** Instead of one
  flat wall of every command, the dialog lists each node with a checkbox
  (Select all / Clear) and a live preview of the selected nodes. When cmedit
  scripts have been generated, Apply reads the `*_cmedit.txt` files from the
  SCRIPTS folder — so edits made after Generate (trimmed lines, fixed values)
  are what gets applied; otherwise it builds fresh from the audit results.
- **Run Scripts dialog is node-centric.** Each entry is labelled by node and
  type (`.mos (moshell)` / `cmedit (cli.py, per-line)`) with Select all / Clear.

## [1.8.2] - 2026-08-06

### Changed
- **Run Scripts now runs cmedit files too, from the folder.** The audit's
  cmedit scripts are executed the same way as `.mos`: Run Scripts lists the
  files in the SCRIPTS folder so they can be edited (or trimmed to a subset)
  first, then each selected file runs — `.mos` via moshell, `*_cmedit.txt`
  per-line via `python cli.py "cmedit set …"`. The button now also appears for
  a GSM-only audit (no `.mos`).
- **cmedit/cmbulk scripts no longer carry a comment header.** The `# ----`
  header (Generate by / Format / Datetime / …) is removed from the cmedit and
  cmbulk exports — the ENM CMEdit CLI and CM Bulk importer error on `#` lines.
  The `.mos` header is unchanged (moshell treats `#` as a comment).

## [1.8.1] - 2026-08-06

### Fixed
- **GSM cmedit/cmbulk scripts now use the real BSC from the source.** The BSC in
  the generated `GeranCell` FDN was only filled from the main form's BSC field,
  leaving a `<BSC>` placeholder when it was blank. The generator now takes the
  BSC from the live cmedit source per cell (the `NodeId` column, captured as
  `__bsc__`), falling back to the CDD `BSC` column and finally the form field.
- **Generate Scripts no longer stops on a GSM-only audit.** When every mismatch
  was GSM/cmedit-sourced (so the runnable `.mos` is empty), the generator
  returned early and produced no cmedit/cmbulk files at all. All three formats
  are now always generated; the Run Scripts button appears only when there is a
  runnable `.mos`.

## [1.8.0] - 2026-08-05

### Added
- **GSM IP-broker audit.** The CDD Audit now verifies each GSM node's
  `AbisIp.bscBrokerIpAddress` (from the dump) against the broker IP expected for
  its BSC. The BSC comes from the CDD `BSC` column; the expected broker IP comes
  from `config.json → bsc_broker_map`. A node wired to the wrong BSC's broker
  (whose ping still succeeds, so the fault is otherwise invisible) is reported as
  a Mismatch, and the actual IP is annotated with the BSC it really points at
  (e.g. expected `10.14.194.131 (MINBS00)` vs actual `10.14.204.3 (MINBS01)`).

### Fixed
- **Aggregate audit rows no longer leak into generated scripts.** TRX-count (and
  the new IP-broker) rows compare a derived value with no single settable MO
  attribute, so they are now excluded from the `.mos`, cmedit, and cmbulk
  scripts instead of emitting malformed `set` lines.
- **Form values are no longer lost when opening CDD Audit or Cut Over.** The
  form was only persisted when launching an Integration run, so entering Audit
  or Cut Over and returning showed an empty form. Both paths now persist the
  form. The CDD Audit page also remembers its own inputs (CDD/LLD/dump files,
  cluster, batch nodes) across navigation via a per-page session state.

## [1.7.4] - 2026-08-05

### Fixed
- **Bundled `audit_map.json` now auto-refreshes on upgrade.** The seeded copy
  next to the exe was only ever written once and never updated, so improvements
  to the CDD mapping (e.g. the GSM per-sector TRX-count profile) were silently
  ignored on machines that already had an older copy — the audit produced no TRX
  rows. Code-owned assets are now re-seeded when a new build ships a changed
  default, while a copy the user has hand-edited is preserved.

### Changed
- **cmedit-sourced params kept out of the runnable `.mos`.** Parameters read
  from live cmedit (BSC `GeranCell`) can't be set via moshell, so they are now
  excluded from the generated `.mos` (Run) script. They still appear in the
  cmedit and cmbulk scripts. Params sourced from modump continue to generate
  `.mos`, cmedit, and cmbulk as before.

## [1.7.3] - 2026-08-03

### Fixed
- **ENM FDN generation for LTE/NR cmedit and cmbulk scripts.** Short
  subnetwork values from the form, such as `T7`, now expand to
  `SubNetwork=ONRM_ROOT_MO_R,SubNetwork=T7`, and rooted MO values are
  normalized so `ManagedElement` is not duplicated in generated set targets.

### Changed
- **Integration setup actions simplified.** Removed the Pre-flight Check,
  Add to Queue, and Queue buttons from the Integration setup page.

## [1.7.2] — 2026-08-02

### Fixed
- **Pin `flet`/`flet-desktop` to 0.84.0.** With `>=`, CI resolved to flet 0.86.5
  and the resulting exe failed to launch on Windows: extracting the newer
  desktop client to `~/.flet/client/flet-desktop-full-0.86.5` raised
  `PermissionError: [WinError 5] Access is denied` on the final rename (AV /
  locked-file races during unpack). The app is developed and tested on 0.84.0,
  which is already cached and validated on target machines, so the build now
  pins to the tested version and matches the local build exactly.

## [1.7.1] — 2026-08-02

### Fixed
- **GitHub Actions release built a broken exe.** `build_assets/flet-windows.zip`
  (the ~40 MB Flet desktop client) is gitignored, so it never reached the CI
  runner — `NodeCraft.spec` then produced a client-less exe that tried to
  download the client from GitHub on first launch and failed to open on
  restricted networks. The release workflow now fetches the exact
  `flet-windows.zip` for the installed flet version before building, so the CI
  artifact is offline and self-contained, identical to the local build.
  `flet-desktop` is now a declared dependency (it was only present locally, so
  CI silently built without the desktop package too).

## [1.7.0] — 2026-08-01

### Fixed
- **Cut Over traffic detection never matched a single cell.** Real `stzrc`
  output abbreviates the MO class — `FDD=…`, `TDD=…`, `DU=…` — but the parser
  only accepted the full `EUtranCellFDD=` / `NRCellDU=` forms. Every run would
  therefore have fallen into the manual-confirmation gate. The short forms are
  now accepted and canonicalised, so `hgetc` names and `stzrc` names resolve to
  the same cell.
- **UE lookup could silently read 0 forever.** Traffic used an exact `mo_ref`
  match while cell status used tolerant matching, so any difference in DN form
  between the two commands looked identical to "no traffic" and stalled the run
  for the full timeout. New `ue_for_cell()` mirrors `match_row` (exact → DN →
  suffix; ambiguity resolves to nothing rather than a guess).

### Added
- **`stzrc` parsed natively.** Its `;`-delimited LTECell/NRCell tables are read
  header-first, giving per-cell state (`S`), UE count, band, alarm indicator and
  the `TABREMDF` flags, plus the `Total: N Cells (M up)` footer as a free sanity
  check. Because `stzrc` carries state *and* traffic, the enable poll now
  sources state from it too (`enable_poll.source`) — one command per poll
  instead of two, which matters on a node that is busy mid-cutover.
- **Rollback.** A **Re-lock** button per band group plus **Roll Back All**, so a
  cutover that isn't working can be reversed from the app instead of hand-typing
  MOs in a terminal at the worst possible moment. It only ever touches cells
  *this run* unlocked, and has its own confirmation listing the literal commands.
- **Pre-state snapshot.** Cell state is captured before anything is sent. A cell
  already unlocked and carrying customers is marked *already in service*, is not
  unlocked, and — the point of the whole thing — is never re-locked by a
  rollback. Such rows render dimmed so they read as "not ours".
- **Diagnosis instead of silent timeouts.** A cell stuck at
  `DEPENDENCY_LOCKED` now triggers a radio check (`st B<band>`) and reports
  *"the B3 radio is locked"* rather than spinning for 15 minutes; a cell that is
  up but **barred** is detected before the traffic wait starts, since no UE can
  ever camp on it.
- **Alarm baseline diff.** `alt` is captured before the first unlock, so the
  evidence screenshot highlights alarms this cut over actually caused instead of
  ones the site already had.
- **EN-DC ordering.** Unlocking NR with no LTE anchor in service now warns
  first, and `Unlock All` sends LTE before NR within a group — otherwise NR
  cells sit at 0 UEs for a reason that is purely ordering and looks like a fault.
- **Traffic needs to be sustained.** A cell is confirmed only after N
  consecutive samples at or above the threshold (default 2), so a transient blip
  no longer ends the gate early.

### Changed
- New `config.json` keys, all editable without a rebuild: `enable_poll.source`,
  `unlock.lock_command_template` / `graceful_lock`,
  `diagnosis.radio_status_template` / `barred_command_template`,
  `traffic.required_consecutive_samples`, `alarm.baseline_before_unlock`,
  `prestate.*`, `endc.*`. Note in the config that `deb`/`bl` is the standard
  moshell pair — `ldeb` is kept because that is what this environment specified.

## [1.6.0] — 2026-08-01

### Added
- **Cut Over.** A new workflow that brings a newly integrated node into service
  by unlocking its cells one band group at a time and proving each group carries
  traffic before moving on. Replaces doing this by hand in a terminal.
  - **Discovery**: SSHes and AMOSes into every node on the form, lists all cells
    with their bands, and sorts them into **LB** (700/900), **MB** (1800/2100)
    and **HB** (2300/2600). One combined list across nodes.
  - **Per-group unlock**: each band group has its own `Unlock` button in its
    section header (so it is never ambiguous which cells a button acts on), plus
    an `Unlock All` that runs LB→MB→HB in order. After unlocking, the app polls
    `st cell` until the cells report enabled, then polls the traffic command
    until the UE column goes non-zero, updating each row live.
  - **Evidence**: once a group is carrying traffic, the traffic and alarm output
    is rendered to a PNG under `LOG/<SHORTCODE>/CUTOVER/`, copied to the
    clipboard, and WhatsApp is opened so the operator can paste and send.
  - **Safety**: a confirmation dialog lists the literal commands before anything
    is sent; one command is issued per MO so no pattern can unlock a whole node
    at once; cells whose band is not mapped to a group are shown but never
    unlocked; and a `dry_run` flag logs commands without sending them.
    Cancelling stops the run but does **not** re-lock cells.
  - **Everything is configurable** in `config.json` under `cutover` — commands,
    band→group mapping, poll intervals and timeouts — so a command spelling can
    be corrected without a rebuild. A `final_verification.steps` list is the
    pluggable slot for the post-cutover checks (empty for now).
  - Parsers are deliberately conservative: both known `st cell` layouts are
    handled, NR multi-band array output (`i[1]`/`i[2]` continuation lines) is
    parsed correctly, an ambiguous status row never resolves to a guess, and an
    unreadable UE column raises a manual-confirmation gate rather than inventing
    a traffic figure.
- `cutover` added as a licensable feature, alongside the existing four.

### Changed
- **Main form buttons regrouped.** They were a single row of five; adding Cut
  Over made six, which overflowed the panel. Now two labelled tiers —
  **Workflows** (Integration Launch, Cut Over, TRFS Launch) and **Tools**
  (Terminal, CDD Audit, Clear Data) — both wrapping, so nothing collides at the
  1080 px window minimum.

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
