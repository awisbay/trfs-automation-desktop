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

# ──────────────────────────────────────────────────────────────────
print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {_failures}")
    sys.exit(1)
print("All cut-over parser checks passed.")
