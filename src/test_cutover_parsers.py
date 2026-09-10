"""
Cut Over parser tests.

Runs without a node, without SSH and without flet:

    python3 src/test_cutover_parsers.py

Fixtures mirror the two ``st`` layouts that actually appear in this repo
(``integration_runner.py`` documents the real node format, ``main.py`` holds
the demo/sample format) plus the ``hgetc`` forms documented in
``band_detector.py``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cutover_model import UNMAPPED
from cutover_parsers import (
    band_prefix_for,
    diff_alarms,
    looks_like_unknown_command,
    match_row,
    parse_alarm_summary,
    parse_barred_state,
    parse_cells_from_hgetc,
    parse_radio_status,
    parse_nr_sector_carrier_refs,
    parse_sdir_vswr,
    parse_gerancell,
    parse_gerancell_states,
    parse_tss,
    parse_gsmsector_list,
    gsm_sector_suffix,
    gsm_band_of,
    gsm_cell_belongs,
    sector_of,
    parse_st_cell_rows,
    parse_stzrc,
    parse_ue_counts,
    st_rows_from_stzrc,
)

BAND_GROUPS = {
    "LB": ["L700", "L800", "L900", "NR700"],
    "MB": ["L1800", "L1900", "L2100", "NR1800", "NR1900", "NR2100"],
    "HB": ["L2300", "L2600", "NR2600", "NR3500"],
}

_failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        _failures.append(name)


# ──────────────────────────────────────────────────────────────────
print("\n[1] parse_cells_from_hgetc — LTE")
LTE_OUT = """
MO                                         freqBand
EUtranCellFDD=TCFGAMANKILAMTAGUMDDNY-11    ;8
EUtranCellFDD=TCFGAMANKILAMTAGUMDDNL-12    ;28
EUtranCellFDD=TCFGAMANKILAMTAGUMDDNF-21    ;3
EUtranCellFDD=TCFGAMANKILAMTAGUMDDNW-22    ;1
EUtranCellTDD=TCFGAMANKILAMTAGUMDDNK-31    ;40
EUtranCellTDD=TCFGAMANKILAMTAGUMDDNV-32    ;41
EUtranCellFDD=TCFGAMANKILAMTAGUMDDNZ-99    ;5

7 MOs found
"""

cells = parse_cells_from_hgetc(LTE_OUT, "", "NODE_A", BAND_GROUPS)
check("7 LTE cells parsed", len(cells) == 7, f"got {len(cells)}")
by_dn = {c.cell_dn: c for c in cells}

check("band 8 -> L900 / LB",
      by_dn["TCFGAMANKILAMTAGUMDDNY-11"].band_key == "L900"
      and by_dn["TCFGAMANKILAMTAGUMDDNY-11"].group == "LB")
check("band 28 -> L700 / LB",
      by_dn["TCFGAMANKILAMTAGUMDDNL-12"].group == "LB")
check("band 3 -> L1800 / MB",
      by_dn["TCFGAMANKILAMTAGUMDDNF-21"].band_key == "L1800"
      and by_dn["TCFGAMANKILAMTAGUMDDNF-21"].group == "MB")
check("band 1 -> L2100 / MB",
      by_dn["TCFGAMANKILAMTAGUMDDNW-22"].group == "MB")
check("band 40 -> L2300 / HB",
      by_dn["TCFGAMANKILAMTAGUMDDNK-31"].group == "HB")
check("band 41 TDD -> L2600 / HB",
      by_dn["TCFGAMANKILAMTAGUMDDNV-32"].band_key == "L2600"
      and by_dn["TCFGAMANKILAMTAGUMDDNV-32"].group == "HB")
check("TDD mo_type preserved (not inferred from band)",
      by_dn["TCFGAMANKILAMTAGUMDDNK-31"].mo_type == "EUtranCellTDD")
check("unknown band 5 -> UNMAPPED, not unlockable",
      by_dn["TCFGAMANKILAMTAGUMDDNZ-99"].group == UNMAPPED
      and not by_dn["TCFGAMANKILAMTAGUMDDNZ-99"].is_unlockable)
check("mo_ref built for commands",
      by_dn["TCFGAMANKILAMTAGUMDDNF-21"].mo_ref
      == "EUtranCellFDD=TCFGAMANKILAMTAGUMDDNF-21")
check("sector uses last digit: 11 -> S1",
      by_dn["TCFGAMANKILAMTAGUMDDNY-11"].sector == "1")
check("sector uses last digit: 12 -> S2",
      by_dn["TCFGAMANKILAMTAGUMDDNL-12"].sector == "2")
check("sector regex can override the default",
      sector_of("999", "SITE-SECTOR7-999", r"SECTOR(?P<sector>\d)") == "7")

# ──────────────────────────────────────────────────────────────────
print("\n[2] parse_cells_from_hgetc — NR, including array continuation lines")
NR_OUT = """
MO                                        bandListManual
NRCellDU=TCFGAMANKILAMTAGUMDDNP-401       ;i[1] = 28
NRCellDU=TCFGAMANKILAMTAGUMDDNN-411       ;i[1] = 41
                                          ;i[2] = 78
NRCellDU=TCFGAMANKILAMTAGUMDDNN-412       ;78

3 MOs found
"""

nr = parse_cells_from_hgetc("", NR_OUT, "NODE_A", BAND_GROUPS)
check("3 NR cells parsed (continuation not a 4th cell)", len(nr) == 3,
      f"got {len(nr)}")
nr_by = {c.cell_dn: c for c in nr}
check("n28 -> NR700 / LB", nr_by["TCFGAMANKILAMTAGUMDDNP-401"].group == "LB")
check("multiband first-policy picks 41 -> NR2600 / HB",
      nr_by["TCFGAMANKILAMTAGUMDDNN-411"].band_key == "NR2600"
      and nr_by["TCFGAMANKILAMTAGUMDDNN-411"].group == "HB")
check("multiband extra band recorded",
      nr_by["TCFGAMANKILAMTAGUMDDNN-411"].extra_band_numbers == [78],
      str(nr_by["TCFGAMANKILAMTAGUMDDNN-411"].extra_band_numbers))
check("bare ';78' form -> NR3500 / HB",
      nr_by["TCFGAMANKILAMTAGUMDDNN-412"].band_key == "NR3500")
check("NR sector also uses last digit: 412 -> S2",
      nr_by["TCFGAMANKILAMTAGUMDDNN-412"].sector == "2")
check("each cell in exactly one group",
      all(c.group in ("LB", "MB", "HB", UNMAPPED) for c in nr))

lowest = parse_cells_from_hgetc("", NR_OUT, "NODE_A", BAND_GROUPS,
                                nr_multiband_policy="lowest")
check("lowest-policy picks 41 over 78",
      {c.cell_dn: c.band_number for c in lowest}["TCFGAMANKILAMTAGUMDDNN-411"] == 41)
highest = parse_cells_from_hgetc("", NR_OUT, "NODE_A", BAND_GROUPS,
                                 nr_multiband_policy="highest")
check("highest-policy picks 78 over 41",
      {c.cell_dn: c.band_number for c in highest}["TCFGAMANKILAMTAGUMDDNN-411"] == 78)

# ──────────────────────────────────────────────────────────────────
print("\n[3] discovery edge cases")
check("empty output -> no cells", parse_cells_from_hgetc("", "", "N", BAND_GROUPS) == [])
check("noise-only output -> no cells",
      parse_cells_from_hgetc("MO  freqBand\n\n0 MOs found\n", "", "N", BAND_GROUPS) == [])

two_nodes = (parse_cells_from_hgetc(LTE_OUT, "", "NODE_A", BAND_GROUPS)
             + parse_cells_from_hgetc(LTE_OUT, "", "NODE_B", BAND_GROUPS))
check("same DN on two nodes stays distinct",
      len({c.key for c in two_nodes}) == 14, str(len({c.key for c in two_nodes})))

trailing = parse_cells_from_hgetc(
    "EUtranCellFDD=SITE-1   ;3 (BAND3)\n", "", "N", BAND_GROUPS)
check("';3 (BAND3)' still parses as band 3",
      trailing and trailing[0].band_number == 3,
      str(trailing[0].band_number) if trailing else "no cells")

excl = parse_cells_from_hgetc(LTE_OUT, "", "N", BAND_GROUPS, include_unmapped=False)
check("include_unmapped=False drops the band-5 cell", len(excl) == 6, f"got {len(excl)}")

# ──────────────────────────────────────────────────────────────────
print("\n[4] parse_st_cell_rows — demo layout (MO first, bare states)")
ST_DEMO = """ Proxy(MO)                                            AdmState  OpState  AvailStatus
 EUtranCellFDD=TCFGAMANKILAMTAGUMDDNY-11             UNLOCKED ENABLED  null
 EUtranCellFDD=TCFGAMANKILAMTAGUMDDNL-12             LOCKED   DISABLED null

2 MOs found
"""
rows = parse_st_cell_rows(ST_DEMO)
check("2 rows parsed", len(rows) == 2, f"got {len(rows)}")
check("header row skipped", all("AdmState" not in r.raw for r in rows))
check("UNLOCKED/ENABLED read",
      rows[0].admin_state == "UNLOCKED" and rows[0].op_state == "ENABLED",
      f"{rows[0].admin_state}/{rows[0].op_state}")
check("LOCKED not matched inside UNLOCKED",
      rows[1].admin_state == "LOCKED" and rows[1].op_state == "DISABLED",
      f"{rows[1].admin_state}/{rows[1].op_state}")

print("\n[5] parse_st_cell_rows — real node layout (MO last, parenthesized)")
ST_REAL = """
2966  1 (UNLOCKED)  1 (ENABLED)   ManagedElement=1,ENodeBFunction=1,EUtranCellFDD=TCFGAMANKILAMTAGUMDDNY-11
2967  0 (LOCKED)    0 (DISABLED)  ManagedElement=1,ENodeBFunction=1,EUtranCellFDD=TCFGAMANKILAMTAGUMDDNL-12
"""
rows2 = parse_st_cell_rows(ST_REAL)
check("2 rows parsed from MO-last layout", len(rows2) == 2, f"got {len(rows2)}")
check("parenthesized UNLOCKED/ENABLED read",
      rows2[0].admin_state == "UNLOCKED" and rows2[0].op_state == "ENABLED",
      f"{rows2[0].admin_state}/{rows2[0].op_state}")
check("comma-joined DN -> last MO wins",
      rows2[0].cell_dn == "TCFGAMANKILAMTAGUMDDNY-11", rows2[0].cell_dn)
check("parenthesized LOCKED/DISABLED read",
      rows2[1].admin_state == "LOCKED" and rows2[1].op_state == "DISABLED")

print("\n[6] match_row")
check("exact match", match_row(cells, "NODE_A", rows[0]) is by_dn["TCFGAMANKILAMTAGUMDDNY-11"])
check("wrong node -> no match", match_row(cells, "NODE_B", rows[0]) is None)
fdd_tdd = parse_st_cell_rows(
    " EUtranCellTDD=TCFGAMANKILAMTAGUMDDNY-11   UNLOCKED ENABLED null\n")
check("dn-mode falls back across MO class",
      match_row(cells, "NODE_A", fdd_tdd[0]) is by_dn["TCFGAMANKILAMTAGUMDDNY-11"])
check("unknown DN -> None",
      match_row(cells, "NODE_A",
                parse_st_cell_rows(" EUtranCellFDD=NOPE-1  UNLOCKED ENABLED null\n")[0])
      is None)

dupes = parse_cells_from_hgetc(
    "EUtranCellFDD=SAME-1 ;3\nEUtranCellTDD=SAME-1 ;41\n", "", "N", BAND_GROUPS)
amb = parse_st_cell_rows(" NRCellDU=SAME-1  UNLOCKED ENABLED null\n")
check("ambiguous DN never guesses", match_row(dupes, "N", amb[0]) is None)

# ──────────────────────────────────────────────────────────────────
print("\n[7] parse_ue_counts")
TRAFFIC = """ Proxy(MO)                                     UE    DL      UL
 EUtranCellFDD=TCFGAMANKILAMTAGUMDDNY-11        14   1200    340
 EUtranCellFDD=TCFGAMANKILAMTAGUMDDNL-12         0      0      0

2 MOs found
"""
res = parse_ue_counts(TRAFFIC)
check("UE column located", res.ok, res.warning)
check("strategy is column_span", res.strategy == "column_span", res.strategy)
check("UE 14 read",
      res.counts.get("EUTRANCELLFDD=TCFGAMANKILAMTAGUMDDNY-11") == 14,
      str(res.counts))
check("UE 0 read (not treated as missing)",
      res.counts.get("EUTRANCELLFDD=TCFGAMANKILAMTAGUMDDNL-12") == 0)

NO_HEADER = """ EUtranCellFDD=SITE-1   14   1200
 EUtranCellFDD=SITE-2    0      0
"""
res2 = parse_ue_counts(NO_HEADER)
check("no UE header -> strategy none (refuses to guess)", res2.strategy == "none")
check("no UE header -> empty counts, not a fabricated number", res2.counts == {})
check("no UE header -> actionable warning", "ue_column_names" in res2.warning)

res3 = parse_ue_counts(NO_HEADER,
                       ue_regex=r"(?P<mo>EUtranCellFDD=\S+)\s+(?P<ue>\d+)")
check("ue_regex override works", res3.strategy == "regex" and
      res3.counts.get("EUTRANCELLFDD=SITE-1") == 14, str(res3.counts))

res4 = parse_ue_counts(TRAFFIC, ue_column_names=("NoOfUsers",))
check("wrong column name -> none, not a wrong number", res4.strategy == "none")

res5 = parse_ue_counts(NO_HEADER, ue_regex="(?P<ue>[")
check("invalid ue_regex -> none + warning",
      res5.strategy == "none" and "not valid" in res5.warning)

# ──────────────────────────────────────────────────────────────────
print("\n[8] alarms + unknown-command guard")
total, by_sev, none_active = parse_alarm_summary(
    "=============  ACTIVE ALARMS  =============\n*** No Active alarms ***\n")
check("no-alarm form detected", none_active and total == 0)

total2, by_sev2, none2 = parse_alarm_summary(
    "Severity  Problem\nCRITICAL  Link failure\nMAJOR     VSWR\nMAJOR     Temp\n")
check("severities counted",
      by_sev2.get("CRITICAL") == 1 and by_sev2.get("MAJOR") == 2, str(by_sev2))
check("total derived when not stated", total2 == 3 and not none2, str(total2))

pats = ["Unknown command", "Syntax error", "command not found"]
check("unknown command detected",
      looks_like_unknown_command("stzrc\nUnknown command: stzrc\n", pats)
      == "Unknown command")
check("normal output not flagged",
      looks_like_unknown_command(TRAFFIC, pats) is None)

# ──────────────────────────────────────────────────────────────────
print("\n[9] parse_stzrc — the real command's table format")
STZRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "testdata", "stzrc_sample.txt")).read()
z = parse_stzrc(STZRC)
zb = {r.mo_ref: r for r in z.rows}
check("12 cell rows parsed", len(z.rows) == 12, str(len(z.rows)))
check("LTE footer says 8 cells / 6 up", z.totals.get("LTE") == (8, 6),
      str(z.totals.get("LTE")))
check("NR footer says 4 cells / 3 up", z.totals.get("NR") == (4, 3),
      str(z.totals.get("NR")))
check("row count matches the footers",
      len(z.rows) == z.totals["LTE"][0] + z.totals["NR"][0])
check("up count matches the footers",
      sum(1 for r in z.rows if r.is_up) == z.totals["LTE"][1] + z.totals["NR"][1])

check("short form FDD= expands to EUtranCellFDD",
      "EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNY-121" in zb)
check("short form TDD= expands to EUtranCellTDD",
      "EUtranCellTDD=TCPHTP3ACANOCOTAGUMDDNK-161" in zb)
check("short form DU= expands to NRCellDU",
      "NRCellDU=TCPHTP3ACANOCOTAGUMDDNP-181" in zb)

check("UE count read from the UEs column",
      zb["EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNF-141"].ue_count == 193,
      str(zb["EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNF-141"].ue_count))
check("S=1 means up",
      zb["EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNY-121"].is_up)
check("S=L means locked, UE 0",
      zb["EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNF-142"].is_locked
      and zb["EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNF-142"].ue_count == 0)
check("UE 0 is a real zero, not 'missing'",
      zb["EUtranCellDU" if False else
         "NRCellDU=TCPHTP3ACANOCOTAGUMDDNP-181"].ue_count == 0)
check("band column read", zb["EUtranCellTDD=TCPHTP3ACANOCOTAGUMDDNV-171"].band == "41")
check("TABREMDF flags captured verbatim",
      zb["EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNY-121"].flags == "--------",
      repr(zb["EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNY-121"].flags))

check("FRU/board rows are not mistaken for cells",
      not any("RRU" in r.cell_dn or "RANP" in r.cell_dn for r in z.rows))
check("subCarrS '15 (LB)' never read as a band",
      all(r.band in ("", "8", "28", "3", "1", "40", "41") for r in z.rows),
      sorted({r.band for r in z.rows}))

zu = parse_ue_counts(STZRC)
check("parse_ue_counts picks the stzrc strategy", zu.strategy == "stzrc", zu.strategy)
check("all 12 cells got a UE count", len(zu.counts) == 12, str(len(zu.counts)))
check("stzrc result handed back for state reuse", zu.stzrc is not None)

zst = st_rows_from_stzrc(z)
check("state adapter yields 9 up / 3 locked",
      sum(1 for r in zst if r.op_state == "ENABLED") == 9
      and sum(1 for r in zst if r.admin_state == "LOCKED") == 3,
      f"{sum(1 for r in zst if r.op_state=='ENABLED')} up")

# The whole point of the short-form fix: hgetc names and stzrc names must
# resolve to the same cell.
hg = parse_cells_from_hgetc(
    "EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNY-121 ;8\n",
    "NRCellDU=TCPHTP3ACANOCOTAGUMDDNP-182 ;i[1] = 28\n", "MIN3117", BAND_GROUPS)
check("hgetc LTE cell matches its stzrc row",
      match_row(hg, "MIN3117", zst[0]) is not None)
nr_row = next(r for r in zst if r.cell_dn.endswith("P-182"))
check("hgetc NRCellDU matches the stzrc DU= row",
      match_row(hg, "MIN3117", nr_row) is not None)

non_stzrc = parse_ue_counts(TRAFFIC)
check("non-stzrc output still uses the generic strategy",
      non_stzrc.strategy == "column_span", non_stzrc.strategy)
check("garbage output still refuses to guess",
      parse_ue_counts("hello world\n").strategy == "none")

# ──────────────────────────────────────────────────────────────────
print("\n[10] diagnosis helpers")
check("cellBarred BARRED -> True",
      parse_barred_state("EUtranCellFDD=X-1 cellBarred BARRED") is True)
check("cellBarred NOT_BARRED -> False",
      parse_barred_state("EUtranCellFDD=X-1 cellBarred NOT_BARRED") is False)
check("absent attribute -> None, never assumed unbarred",
      parse_barred_state("EUtranCellFDD=X-1 someOtherAttr 3") is None)

radio = parse_radio_status(
    " Proxy(MO)      AdmState  OpState\n"
    " Carrier=B3     LOCKED    DISABLED\n"
    " Carrier=B3-2   UNLOCKED  ENABLED\n")
check("radio status counts locked/disabled",
      radio["total"] == 2 and radio["locked"] == 1 and radio["disabled"] == 1,
      str(radio))

new_alarms = diff_alarms(
    "1 ;MAJOR ;VSWR on branch A\n",
    "1 ;MAJOR ;VSWR on branch A\n2 ;CRITICAL ;Cell out of service\n")
check("alarm diff returns only the new alarm",
      len(new_alarms) == 1 and "Cell out of service" in new_alarms[0],
      str(new_alarms))
check("alarm diff is empty when nothing changed",
      diff_alarms("1 ;MAJOR ;VSWR\n", "1 ;MAJOR ;VSWR\n") == [])

check("band_prefix_for inverts the prefix map",
      band_prefix_for("L1800") == "F" and band_prefix_for("NR2600") == "N",
      f"{band_prefix_for('L1800')}/{band_prefix_for('NR2600')}")
check("unknown band has no prefix", band_prefix_for("L9999") == "")

print("\n[11] parse_sdir_vswr — per-RF-port VSWR from sdirc")
_vswr_sample = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "testdata", "sdirc_vswr_sample.txt")
with open(_vswr_sample, "r", encoding="utf-8") as _fh:
    _vswr_out = _fh.read()
vres = parse_sdir_vswr(_vswr_out)
check("sdirc VSWR parses ok", vres.ok, vres.warning)
rru1 = vres.by_cell.get("CMPBAHIANMALAYBBUKL-171", [])
check("cell 171 mapped to its 4 RF ports (A-D)",
      sorted(p["port"] for p in rru1) == ["A", "B", "C", "D"],
      str([p["port"] for p in rru1]))
check("cell 171 port A VSWR = 1.13",
      any(p["port"] == "A" and abs(p["vswr"] - 1.13) < 1e-9 for p in rru1),
      str(rru1))
check("cell 171 worst VSWR is the 1.30 on port D",
      abs(max(p["vswr"] for p in rru1) - 1.30) < 1e-9, str(rru1))
check("NR cell 501 (NRC= in sdirc) is mapped by DN",
      len(vres.by_cell.get("CMPBAHIANMALAYBBUKP-501", [])) == 4)
check("AAS radio with '-' VSWR yields None values, not a guess",
      all(p["vswr"] is None
          for p in vres.by_cell.get("CMPBAHIANMALAYBBUKG-L1", [])),
      str(vres.by_cell.get("CMPBAHIANMALAYBBUKG-L1")))
check("empty output fails loudly", parse_sdir_vswr("").ok is False)

_gsm_vswr = """
FRU ;LNH ;BOARD ;RF ;BP ;TX (W/dBm) ;VSWR (RL) ;RX (dBm) ;UEs/gUEs ;Sector/AntennaGroup/Cells (State:CellIds:PCIs)
RRU1 ;BXP_1 ;RADIO ;A ;11 ;41.8 (46.2) ;1.10 (26.5) ;-83.9 ;-/- ;SE=X GT=GINGOO-L1/0 GT=GINGOO-L1/1 (1,1)
RRU2 ;BXP_2 ;RADIO ;B ;11 ;40.5 (46.1) ;1.08 (28.0) ;-78.0 ;-/- ;SE=Y GT=GINGOO-2/2 GT=GINGOO-2/3 (1,1)
-----
"""
gsm_vres = parse_sdir_vswr(_gsm_vswr)
check("GSM GT layer token maps to VSWR port",
      gsm_vres.by_gsm_token["L1"][0]["vswr"] == 1.10,
      str(gsm_vres.by_gsm_token))
check("GSM numeric GT sector token maps to VSWR port",
      gsm_vres.by_gsm_token["2"][0]["port"] == "B",
      str(gsm_vres.by_gsm_token))

print("\n[12] GSM — GeranCell states, tss, lst gsmsector, sector linkage")
_geran_active = (
    "NodeId  BscFunctionId   BscMId  GeranCellMId    GeranCellId     geranCellId     state\n"
    "MINBS00 1       1       1       M2839S3 M2839S3 ACTIVE\n"
    "MINBS00 1       1       1       M2839S2 M2839S2 ACTIVE\n"
    "MINBS00 1       1       1       M2839S1 M2839S1 ACTIVE\n")
gstates = parse_gerancell_states(_geran_active, "MIN283")
check("gerancell states: 3 site cells parsed",
      len(gstates) == 3 and gstates.get("M2839S1") == "ACTIVE", str(gstates))

_geran_halted = (
    "NodeId  BscFunctionId   BscMId  GeranCellMId    GeranCellId     geranCellId     state\n"
    "MINVBS02        1       1       1       M33479S1        M33479S1        HALTED\n"
    "MINVBS02        1       1       1       M33479S3        M33479S3        HALTED\n")
hstates = parse_gerancell_states(_geran_halted, "MIN3347")
check("gerancell HALTED parsed, header ignored",
      hstates.get("M33479S1") == "HALTED" and "GERANCELLID" not in hstates,
      str(hstates))
check("foreign site cell is filtered out",
      parse_gerancell_states(_geran_active, "MIN999") == {})

_geran_verbose = (
    "FDN : SubNetwork=ONRM_ROOT_MO_R,SubNetwork=T7,SubNetwork=BSC,MeContext=MINBS01,"
    "ManagedElement=MINBS01,BscFunction=1,BscM=1,GeranCellM=1,GeranCell=M8239R3\n"
    "geranCellId : M8239R3\n"
    "state : HALTED\n\n"
    "FDN : SubNetwork=ONRM_ROOT_MO_R,SubNetwork=T7,SubNetwork=BSC,MeContext=MINBS01,"
    "ManagedElement=MINBS01,BscFunction=1,BscM=1,GeranCellM=1,GeranCell=M8239S2\n"
    "geranCellId : M8239S2\n"
    "state : HALTED\n")
gv = parse_gerancell(_geran_verbose, "MIN823")
check("verbose get captures FDN + state",
      gv["M8239R3"]["state"] == "HALTED"
      and gv["M8239R3"]["fdn"].endswith("GeranCell=M8239R3"),
      str(gv.get("M8239R3")))
check("verbose FDN is the full BSC path (needed for cmedit set)",
      gv["M8239R3"]["fdn"].startswith("SubNetwork=ONRM_ROOT_MO_R"))

check("sector suffix: M2839S1 -> 1 (GSM900)",
      gsm_sector_suffix("M2839S1", "MIN283") == "1"
      and gsm_band_of("M2839S1", "MIN283") == "GSM900")
check("sector suffix: M8239R3 -> 3",
      gsm_sector_suffix("M8239R3", "MIN823") == "3")
check("GSM layer letters collapse into physical sectors",
      {gsm_sector_suffix(cell, "MIN823")
       for cell in ("M8239L1", "M8239R1", "M8239S1")} == {"1"})
check("gsm_cell_belongs rejects a longer foreign digit run",
      gsm_cell_belongs("M2839S1", "MIN283") and not gsm_cell_belongs("M28399S1", "MIN283"))

_tss = (
    "GsmSector=CMPBAHIANMALAYBBUK-1,Trx=0  abisTsState  i[8] = 2 2 2 2 2 2 2 2 "
    "(ENABLED ENABLED ENABLED ENABLED ENABLED ENABLED ENABLED ENABLED)\n"
    "GsmSector=CMPBAHIANMALAYBBUK-1,Trx=1  abisTsState  i[8] = 2 2 2 2 2 2 2 2 "
    "(ENABLED ENABLED ENABLED ENABLED ENABLED ENABLED ENABLED ENABLED)\n"
    "GsmSector=CMPBAHIANMALAYBBUK-R3,Trx=0 abisTsState  i[8] = 2 2 0 2 2 2 2 2 "
    "(ENABLED ENABLED DISABLED ENABLED ENABLED ENABLED ENABLED ENABLED)\n")
tss = parse_tss(_tss)
check("tss sector 1 all enabled", tss["1"]["all_enabled"] is True, str(tss.get("1")))
check("tss sector R3 normalises to sector 3",
      tss["3"]["all_enabled"] is False, str(tss.get("3")))

_lst = (
    "    4  1 (UNLOCKED)  1 (ENABLED)   BtsFunction=1,GsmSector=CMPBAHIANMALAYBBUK-1,Trx=0\n"
    "    5  1 (UNLOCKED)  0 (DISABLED)  BtsFunction=1,GsmSector=CMPBAHIANMALAYBBUK-1,Trx=1\n"
    "    3  1 (UNLOCKED)  1 (ENABLED)   BtsFunction=1,GsmSector=CMPBAHIANMALAYBBUK-1,AbisIp=1\n")
lst = parse_gsmsector_list(_lst)
check("lst gsmsector keeps Trx rows, skips AbisIp",
      set(lst.get("1", {}).keys()) == {"0", "1"}, str(lst))
check("lst gsmsector Trx1 op DISABLED captured",
      lst["1"]["1"]["op"] == "DISABLED" and lst["1"]["0"]["op"] == "ENABLED",
      str(lst.get("1")))

# ──────────────────────────────────────────────────────────────────
print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {_failures}")
    sys.exit(1)
print("\n[13] NRCellDU sectorCarrierRef")
_nr_carriers = """
NRCellDU=GINGOON-401 nRSectorCarrierRef [1] =
 >>> nRSectorCarrierRef = GNBDUFunction=1,NRSectorCarrier=N41_S1
NRCellDU=GINGOON-402 nRSectorCarrierRef [1] =
 >>> nRSectorCarrierRef = GNBDUFunction=1,NRSectorCarrier=N41_S2
NRCellDU=GINGOON-403 nRSectorCarrierRef [1] =
 >>> nRSectorCarrierRef = GNBDUFunction=1,NRSectorCarrier=N41_S3
"""
nr_refs = parse_nr_sector_carrier_refs(_nr_carriers)
check("all three live carrier references parsed", len(nr_refs) == 3, str(nr_refs))
check("NRCellDU 401 maps to the node-provided N41_S1 reference",
      nr_refs.get("GINGOON-401") ==
      "GNBDUFunction=1,NRSectorCarrier=N41_S1", str(nr_refs))

print("All cut-over parser checks passed.")
