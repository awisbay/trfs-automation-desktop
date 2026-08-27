"""
ess_audit.py — ESS (E-UTRAN/NR Spectrum Sharing) pairing audit.

The CDD's ``ESS`` sheet pairs an LTE cell with an NR cell and gives the expected
``essScLocalId`` / ``essScPairId``. For each pair (of the audited node) this
checks, against the node dump:

  1. the LTE cell and NR cell both EXIST on the node;
  2. ``essScLocalId`` / ``essScPairId`` are present AND EQUAL on the LTE
     ``SectorCarrier`` and the NR ``NRSectorCarrier`` (and equal the sheet);
  3. ``essEnabled = true`` on the LTE→NR ``GUtranCellRelation`` and the
     NR→LTE ``EUtranCellRelation``.

Pattern (calibrated on a real dump), e.g. LTE cell GFATIML-171 ↔ NR GFATIMP-501:
  SectorCarrier=B28_S1    essScLocalId=171  essScPairId=5010000000171
  NRSectorCarrier=N28_S1  essScLocalId=171  essScPairId=5010000000171
  EUtranCellFDD=GFATIML-171,...,GUtranCellRelation=5152-<gNBID>-501  essEnabled=true
  NRCellCU=GFATIMP-501,EUtranCellRelation=GFATIML-171               essEnabled=true
``essScPairId`` = NR LocalCellID followed by the LTE LocalCellID zero-padded to
10 digits (501 + 0000000171 → 5010000000171).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

import openpyxl


@dataclass
class EssResult:
    node: str
    lte_cell: str
    nr_cell: str
    lte_exists: bool
    nr_exists: bool
    # essScLocalId equals the cell's own local id: on the LTE side it is the
    # EUtranCell cellId, on the NR side the NRCell cellLocalId — so the two
    # sides carry DIFFERENT expected values (e.g. LTE 171 vs NR 501). It is
    # checked THREE ways: CDD == node cellId/cellLocalId == node essScLocalId.
    lte_local_exp: str      # expected LTE essScLocalId = LTE cellId (from CDD)
    lte_cellid: str         # EUtranCell.cellId on the node
    sc_local: str           # SectorCarrier essScLocalId (node)
    nr_local_exp: str       # expected NR essScLocalId = NR cellLocalId (from CDD)
    nr_localid: str         # NRCell.cellLocalId on the node
    nrsc_local: str         # NRSectorCarrier essScLocalId (node)
    ess_pair: str           # expected essScPairId (from CDD, same both sides)
    sc_pair: str            # SectorCarrier essScPairId (node)
    nrsc_pair: str          # NRSectorCarrier essScPairId (node)
    ess_lte: str            # essEnabled on the LTE GUtranCellRelation
    ess_nr: str             # essEnabled on the NR EUtranCellRelation
    status: str             # Match / Mismatch


def _norm(v):
    return "" if v is None else str(v).strip()


def read_ess_pairs(path: str, node: str, log=lambda m: None) -> List[dict]:
    """Read the CDD ``ESS`` sheet → pair dicts for the audited node.

    Header (row 1): eNodeBName, ENodeBID, CellName(LTE), LocalCellID(LTE),
    gNB Name, gNB ID, CellName(NR), LocalCellID(NR), NRSectorCarrier,
    NRSectorCarrier.essScLocalId, NRSectorCarrier.essScPairId, ..."""
    from .sheet_reader import open_sheet
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    # Layers 1–5 (sheet lookup, header detection, alias/normalized columns,
    # guarded reads, diagnostics) all live in open_sheet now.
    sv = open_sheet(
        wb, "ESS",
        required=[
            ("enodebname",), ("cellname",), ("localcellid",),
            # LTE CDD: "SectorCarrier.*"; NSA CDD: "NRSectorCarrier.*".
            ("nrsectorcarrier.esssclocalid", "sectorcarrier.esssclocalid"),
            ("nrsectorcarrier.essscpairid", "sectorcarrier.essscpairid"),
        ],
        header_hint=1, log=log, tag="ess")
    if sv is None:
        wb.close()
        return []
    # CellName / LocalCellID appear twice (LTE then NR) — take positionally.
    cell_is = sv.cols("cellname")
    local_is = sv.cols("localcellid")
    enb_i = sv.col("enodebname")
    gnb_i = sv.col("gnb name")
    idx = {
        "enb": enb_i,
        "gnb": gnb_i,
        "gnb_id": sv.col("gnb id"),
        "lte_cell": cell_is[0] if len(cell_is) > 0 else None,
        "lte_local": local_is[0] if len(local_is) > 0 else None,
        "nr_cell": cell_is[1] if len(cell_is) > 1 else None,
        "nr_local": local_is[1] if len(local_is) > 1 else None,
        "ess_local": sv.col("nrsectorcarrier.esssclocalid",
                            "sectorcarrier.esssclocalid"),
        "ess_pair": sv.col("nrsectorcarrier.essscpairid",
                           "sectorcarrier.essscpairid"),
    }
    node_l = node.strip().lower()
    pairs = []
    for r in sv.rows:
        enb = sv.get(r, idx["enb"])
        gnb = sv.get(r, idx["gnb"])
        if node_l not in (enb.lower(), gnb.lower()):
            continue
        pairs.append({
            "node": enb or gnb,
            "lte_cell": sv.get(r, idx["lte_cell"]),
            "lte_local": sv.get(r, idx["lte_local"]),
            "gnb_id": sv.get(r, idx["gnb_id"]),
            "nr_cell": sv.get(r, idx["nr_cell"]),
            "nr_local": sv.get(r, idx["nr_local"]),
            "ess_local": sv.get(r, idx["ess_local"]),
            "ess_pair": sv.get(r, idx["ess_pair"]),
        })
    wb.close()
    return pairs


_LTE_CELL = re.compile(r"(?:^|,)EUtranCell(?:FDD|TDD)=([^,]+)$")
_NR_CELL = re.compile(r"(?:^|,)NRCell(?:CU|DU)=([^,]+)$")
_SHARING_GROUP = re.compile(r"SpectrumSharingFunction=\d+,SharingGroup=\d+$")


def count_sharing_groups(records: Dict[str, Dict[str, str]], node: str) -> int:
    """Count ``SpectrumSharingFunction=1,SharingGroup=N`` MOs on ``node`` — this
    should equal the number of ESS pairs (one sharing group per ESS cell)."""
    node_l = node.strip().lower()
    n = 0
    for ldn in records:
        if _SHARING_GROUP.search(ldn):
            m = re.search(r"ManagedElement=([^,]+)", ldn)
            if m and m.group(1).lower() == node_l:
                n += 1
    return n


def audit_ess(pairs: List[dict], records: Dict[str, Dict[str, str]],
              log=lambda m: None) -> List[EssResult]:
    """Audit the ESS pairs against the node dump records."""
    if not pairs:
        return []
    # Existing cells on the node, plus each cell's own local id.
    lte_cells, nr_cells = set(), set()
    lte_cellid: Dict[str, str] = {}   # LTE cell → EUtranCell.cellId
    nr_localid: Dict[str, str] = {}   # NR cell  → NRCell.cellLocalId
    # essScLocalId → essScPairId, per carrier type (non-zero only).
    sc_pair_by_local: Dict[str, str] = {}
    nrsc_pair_by_local: Dict[str, str] = {}
    # essEnabled lookups.
    ess_lte: Dict[tuple, str] = {}   # (lteCell, gNBID, nrLocalId) → essEnabled
    ess_nr: Dict[tuple, str] = {}    # (nrCell, lteCell)           → essEnabled

    for ldn, a in records.items():
        m = _LTE_CELL.search(ldn)
        if m:
            lte_cells.add(m.group(1))
            cid = _norm(a.get("cellId"))
            if cid:
                lte_cellid[m.group(1)] = cid
        m = _NR_CELL.search(ldn)
        if m:
            nr_cells.add(m.group(1))
            cid = _norm(a.get("cellLocalId"))
            if cid:
                nr_localid[m.group(1)] = cid
        leaf = ldn.split(",")[-1].split("=", 1)[0]
        if leaf == "SectorCarrier":
            sl = _norm(a.get("essScLocalId"))
            if sl and sl != "0":
                sc_pair_by_local.setdefault(sl, _norm(a.get("essScPairId")))
        elif leaf == "NRSectorCarrier":
            sl = _norm(a.get("essScLocalId"))
            if sl and sl != "0":
                nrsc_pair_by_local.setdefault(sl, _norm(a.get("essScPairId")))
        ess = a.get("essEnabled")
        if ess is not None:
            lm = re.search(r"EUtranCell(?:FDD|TDD)=([^,]+)", ldn)
            # GUtranCellRelation id = "<x>-<gNBID padded>-<nrLocalId>"; match the
            # specific gNB so a neighbour's relation ending in the same localid
            # can't clobber this pair's essEnabled.
            gr = re.search(r"GUtranCellRelation=\d+-0*(\d+)-(\d+)$", ldn)
            if lm and gr:
                ess_lte[(lm.group(1), gr.group(1), gr.group(2))] = \
                    _norm(ess).lower()
            nm = re.search(r"NRCell(?:CU|DU)=([^,]+)", ldn)
            er = re.search(r"EUtranCellRelation=([^,]+)$", ldn)
            if nm and er:
                ess_nr[(nm.group(1), er.group(1))] = _norm(ess).lower()

    out: List[EssResult] = []
    for p in pairs:
        lte_cell, nr_cell = p["lte_cell"], p["nr_cell"]
        ess_pair = p["ess_pair"]
        # Expected per-side local id = the cell's own local id from the CDD.
        lte_local_exp = p["lte_local"]     # LTE cellId
        nr_local_exp = p["nr_local"]       # NR cellLocalId
        lte_exists = lte_cell in lte_cells
        nr_exists = nr_cell in nr_cells
        # The LTE SectorCarrier's essScLocalId must equal the LTE cellId; the NR
        # NRSectorCarrier's must equal the NR cellLocalId. Look each side up by
        # its OWN expected id.
        sc_local = lte_local_exp if lte_local_exp in sc_pair_by_local else ""
        nrsc_local = nr_local_exp if nr_local_exp in nrsc_pair_by_local else ""
        sc_pair = sc_pair_by_local.get(lte_local_exp, "")
        nrsc_pair = nrsc_pair_by_local.get(nr_local_exp, "")
        # The node's own cell local id — the third leg of the check.
        node_lte_id = lte_cellid.get(lte_cell, "")
        node_nr_id = nr_localid.get(nr_cell, "")
        e_lte = ess_lte.get((lte_cell, p["gnb_id"], p["nr_local"]), "")
        e_nr = ess_nr.get((nr_cell, lte_cell), "")

        # Three-way: CDD expected == node cellId/cellLocalId == node essScLocalId.
        ok = (lte_exists and nr_exists
              and sc_local == lte_local_exp and node_lte_id == lte_local_exp
              and nrsc_local == nr_local_exp and node_nr_id == nr_local_exp
              and sc_pair == ess_pair and nrsc_pair == ess_pair
              and e_lte == "true" and e_nr == "true")
        out.append(EssResult(
            node=p["node"], lte_cell=lte_cell, nr_cell=nr_cell,
            lte_exists=lte_exists, nr_exists=nr_exists,
            lte_local_exp=lte_local_exp, lte_cellid=node_lte_id, sc_local=sc_local,
            nr_local_exp=nr_local_exp, nr_localid=node_nr_id, nrsc_local=nrsc_local,
            ess_pair=ess_pair, sc_pair=sc_pair, nrsc_pair=nrsc_pair,
            ess_lte=e_lte or "(none)", ess_nr=e_nr or "(none)",
            status="Match" if ok else "Mismatch"))
    return out
