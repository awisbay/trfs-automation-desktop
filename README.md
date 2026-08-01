# NodeCraft

**Desktop automation for Ericsson RAN node integration (LTE / NR / GSM) and CDD auditing.**

NodeCraft is a Windows desktop tool that streamlines end-to-end integration of
Ericsson RAN nodes via an AMOS gateway / ENM, and audits live node configuration
against planning **CDD** (Cell Design Data) workbooks. It is a single, offline,
self-contained executable built with [Flet](https://flet.dev) (UI) +
[Paramiko](https://www.paramiko.org/) (SSH) and packaged with PyInstaller.

> Version lives in [`src/version.py`](src/version.py) — see [CHANGELOG.md](CHANGELOG.md).

---

## Table of contents

- [Features](#features)
- [The two workflows](#the-two-workflows)
  - [1. Integration](#1-integration)
  - [2. CDD Audit](#2-cdd-audit)
- [CDD Audit — how the mapping works](#cdd-audit--how-the-mapping-works)
  - [`audit_map.json` reference](#audit_mapjson-reference)
  - [Node source: dumps vs live cmedit](#node-source-dumps-vs-live-cmedit)
  - [Output: Excel report + moshell scripts](#output-excel-report--moshell-scripts)
- [Project structure](#project-structure)
- [Running from source](#running-from-source)
- [Building the executable](#building-the-executable)
- [Configuration files](#configuration-files)
- [Versioning](#versioning)
- [Publishing a release](#publishing-a-release)
- [License](#license)

---

## Features

- **Node integration** over SSH/AMOS/ENM: SW-level check, dumps (modump/cmdump),
  baseline, relation scripts, URI reconfig, GSM SGW/broker checks, per-node
  resume, Excel reporting.
- **CDD Audit** (config-driven): compare a node's actual configuration against
  LTE / NR / GSM CDD workbooks at **node**, **cell**, and **relation** level and
  export a colour-coded Excel report.
- **moshell script generation**: turn audit mismatches into ready-to-run
  `set` scripts, one per node.
- **Single-file, offline exe** — bundles the Flet desktop client; no runtime
  download, no Python install required on the target machine.

---

## The two workflows

### 1. Integration

Fill in the site identity, node(s), and ENM/SSH credentials on the main form,
point at your TRFS command file, and run. NodeCraft drives the AMOS session:
SW-level verification, taking modump/cmdump, applying baseline and relation
scripts, URI/broker checks, and writing a per-run Excel report under
`LOG/<SiteID>/`.

### 2. CDD Audit

Open **CDD Audit** from the main form. Enter the **Site ID**, browse the
relevant CDD workbook(s) (LTE / NR / GSM), and Run. NodeCraft:

1. Auto-finds node dumps in `LOG/<SiteID>/DUMP/` (or use browsed dump files —
   cmdump **and** modump are auto-detected). Multiple nodes are supported.
2. For **GSM**, queries the BSC **live via `cmedit`** over the SSH session from
   the main form (scoped by Site ID — no moshell login).
3. Compares every mapped parameter against the config-driven mapping
   (`audit_map.json`).
4. Exports an Excel report (Summary + Detail) and, on mismatches, can
   **Generate Scripts**.

---

## CDD Audit — how the mapping works

All audit behaviour is driven by **`audit_map.json`** (bundled in the exe and
seeded next to it on first run — editable without a rebuild; re-read on every
Run). Each *profile* maps one CDD **sheet** to one managed-object (MO) template.

### `audit_map.json` reference

```jsonc
{
  "profiles": [
    {
      "name": "LTE Cell (EUtranCellFDD)",  // label shown in the log
      "tech": "lte_nr",                     // which CDD file: lte_nr | nr | gsm
      "category": "cell",                   // node | cell | relation
      "sheet": "CDD",                       // Excel sheet name
      "header_row": 3,                      // 1-based header row
      "node_key_column": "eNodeBName",      // column that scopes rows to a node
      "mo_fdn": "ENodeBFunction=1,{_cellmo}={CellName}",  // MO FDN template
      "columns": [
        { "cdd": "TAC", "attr": "tac" },    // CDD column  <>  MO attribute
        { "cdd": "PCI", "attr": "physicalLayerCellId" }
      ]
    }
  ]
}
```

**Templates** — `{ColumnHeader}` in `mo_fdn`/`mo` is replaced with that row's
value. A handful of computed placeholders and per-column options add flexibility:

| Feature | Where | What it does |
|---|---|---|
| `mo` (per column) | column | Override the MO for a single parameter (e.g. a child MO). |
| `cell_mo_map` | profile | Pick the cell MO class from a band column — e.g. `L26`/`L23` → `EUtranCellTDD`, else `EUtranCellFDD` (`{_cellmo}`). |
| `cell_expand` | profile | Expand a relation row keyed by **band** (e.g. `EUTRANCell = L18`) into every real cell of that band on the node (`{_cell}`). |
| `via_ref` | column | Follow a reference attribute (e.g. `sectorCarrierRef`) to the **real** MO (SectorCarrier) and audit its attribute there. The report/script then point at the real MO with a **Reference Cell** column. |
| `split` | column | Derive several real params from one CDD value — e.g. MIMO `32T32R` → `noOfTxAntennas=32`, `noOfRxAntennas=32`. |
| `norm: "segments"` | column | Per-segment numeric normalization — e.g. CGI `515-02-00087-60031` == `515-02-87-60031`. |

Adding a parameter is usually just one line in `columns` — **no code change**.

### Node source: dumps vs live cmedit

- **LTE / NR** values come from parsed **modump** (DCG log) or **cmdump**
  (3GPP XML) files. Both formats are auto-detected. When both exist for a node,
  **cmdump wins**.
- **GSM** values are read **live** via `cmedit get *BS* GeranCell.(...) -t`
  through the prepared SSH session (BSC-level), scoped by the Site ID.

### Output: Excel report + moshell scripts

- **Excel** — a styled **Summary** (colour KPIs, Share %, Compliance) and a
  **Detail** sheet: `Category, Node, Reference Cell, MO, Parameter,
  CDD (expected), Node (actual), Status, Source`. Status is colour-coded
  (Match / Mismatch / NotFound / MO_NotFound).
- **Generate Scripts** — one moshell `set` script **per node**, containing only
  **Mismatch** rows, using the CDD-expected values, grouped by top-level MO and
  sorted by parameter (so a parameter's lines are contiguous and easy to drop).
  Written to `LOG/<SiteID>/AUDIT/SCRIPTS/`.

---

## Project structure

```
NodeCraft/
├─ src/
│  ├─ gui_app.py            # entry point, routing, license gate
│  ├─ version.py            # single source of truth for the app version
│  ├─ integration_runner.py # SSH/AMOS orchestration for integration
│  ├─ audit_map.json        # CDD audit mapping (config-driven)
│  ├─ audit/                # CDD audit engine
│  │  ├─ dump_parser.py     # modump (DCG) + cmdump (XML) → {ldn: {attr: val}}
│  │  ├─ cdd_reader.py      # CDD sheet → AuditItems (driven by audit_map.json)
│  │  ├─ cmedit_source.py   # live GSM values via cmedit -t
│  │  └─ audit_core.py      # compare + Excel report + moshell script gen
│  └─ gui/
│     ├─ form_page.py       # main configuration form
│     ├─ audit_page.py      # CDD Audit page
│     └─ integration_page.py
├─ NodeCraft.spec           # PyInstaller build recipe (onefile)
├─ CHANGELOG.md
└─ README.md
```

---

## Running from source

Requires Python 3.11 and the project virtualenv.

```bash
# from the project root
venv\Scripts\python.exe src\gui_app.py
```

---

## Building the executable

```bash
venv\Scripts\pyinstaller.exe --noconfirm --clean NodeCraft.spec
```

Output: `dist\NodeCraft.exe` (single, self-contained, ~140 MB).

On first launch the exe seeds user-editable files next to itself
(`config.yaml`, `config.json`, `audit_map.json`, `TEMPLATE_REPORT.xlsx`,
`TRFS commands.txt`, `snapshot.ico`) — only if they don't already exist, so your
edits are never overwritten.

**Copying the exe elsewhere:** it is standalone, but put it in a **writable**
folder (it creates `LOG/` and seeds files). A fresh copy will show the
activation screen unless you also copy `license.key`, and `config.yaml` will be
re-seeded blank unless you copy your own.

---

## Configuration files

| File | Purpose | In git? |
|---|---|---|
| `config.yaml` | Site ID + **SSH host/credentials** | **No** — gitignored (secrets) |
| `config.json` | Integration script paths, ENM endpoints, BSC broker map, target UP | Yes |
| `audit_map.json` | CDD audit mapping | Yes (`src/audit_map.json`) |
| `license.key` | Signed per-user license | **No** — gitignored |

---

## Versioning

The version is defined once in [`src/version.py`](src/version.py) and read
everywhere (window title, in-app label, and the exe's Windows file metadata via
`NodeCraft.spec`). Bump it and add a [CHANGELOG.md](CHANGELOG.md) entry on every
release: **PATCH** = fixes, **MINOR** = new feature, **MAJOR** = breaking change.

## Publishing a release

Every official build is published at the canonical
[GitHub Releases page](https://github.com/awisbay/trfs-automation-desktop/releases).
After the version and changelog are committed to `main`, push a matching tag:

```bash
git tag v1.7.1
git push origin v1.7.1
```

The tag must match `src/version.py`. GitHub Actions then runs the tests, builds
the Windows executable, and creates the GitHub Release with
`NodeCraft-<version>-windows.exe`. A release is not complete until the workflow
succeeds and that file is visible on the GitHub Releases page.

---

## License

Proprietary — internal tooling. Not for redistribution.
