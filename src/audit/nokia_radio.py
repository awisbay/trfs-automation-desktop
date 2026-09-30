"""
nokia_radio.py — per radio / RF port / band view of a Nokia IM snapshot:
VSWR plus the uplink level of every cell (LTE RTWP per RX branch) and GSM TRX
(RSSI per RX branch) that uses that port and band.

Where the data lives (all read with ims2_reader):

  VSWR   EQM_L/RMOD_L-x/RU_L-1/VUBUS_L-<port>/VU_L-1/VSWR-k
         rp1Name 'antenna<port><pipe>' (pipe a/b of a dual-band radio),
         vswr in 0.1 steps, with its own minor/major limits
  band   EQM_L/RMOD_L-x/RU_L-1/FF_L-n/FFU_L-1 centre frequency; pipe 'a' is
         the band of the first filters, 'b' the next band (checked against
         WebEM: AHPDA a=B8 b=B28, AHEGC a=B3 b=B1)
  radio  RMOD_L-x is numbered differently from the runtime/plan RMOD_R/RMOD_A
         — they are matched on the radio serial number
  cells  plan CELLMAPPING LCELL_A-<lcr> (LTE) / LCELC_A-<id> (GSM)
         CHANNELGROUP_A-g/CHANNEL_A-c antlDN -> RMOD_A-k/ANTL_A-<port>
  RX     MCTRL LNBTS_M/CELL_M-<lcr>/…/RTWP_MEASUREMENT  (0.1 dBm)
         MCTRL GNBTS_M/CELL_M-<id*1000+trx>/…/RSSI_MEASUREMENT  (dBm)
         CHANNEL_GROUP_M-g/CHANNEL_M-c mirror the plan channel indexes
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

RX_HIGH_DBM = -95.0          # uplink level flagged as high (interference)
VSWR_WARN = 1.4              # VSWR flagged from here, below the radio's minor
BRANCH_IMBALANCE_DB = 3.0    # max-min across a cell's RX branches

_RV = "/RUNTIME_VIEW-1/"

# E-UTRA downlink ranges: band -> (MHz low, MHz high), (EARFCN low, high)
_BANDS = {
    1: ((2110, 2170), (0, 599)),
    2: ((1930, 1990), (600, 1199)),
    3: ((1805, 1880), (1200, 1949)),
    4: ((2110, 2155), (1950, 2399)),
    5: ((869, 894), (2400, 2649)),
    7: ((2620, 2690), (2750, 3449)),
    8: ((925, 960), (3450, 3799)),
    12: ((729, 746), (5010, 5179)),
    20: ((791, 821), (6150, 6449)),
    26: ((859, 894), (8690, 9039)),
    28: ((758, 803), (9210, 9659)),
    38: ((2570, 2620), (37750, 38249)),
    40: ((2300, 2400), (38650, 39649)),
    41: ((2496, 2690), (39650, 41589)),
}
_FREQ_ORDER = (1, 3, 8, 28, 5, 7, 40, 41, 38, 20, 2, 4, 12, 26)


def band_of_freq(hz) -> Optional[int]:
    try:
        mhz = float(hz) / 1e6
    except (TypeError, ValueError):
        return None
    for b in _FREQ_ORDER:
        lo, hi = _BANDS[b][0]
        if lo <= mhz <= hi:
            return b
    return None


def band_of_earfcn(earfcn) -> Optional[int]:
    try:
        n = int(earfcn)
    except (TypeError, ValueError):
        return None
    for b, (_f, (lo, hi)) in _BANDS.items():
        if lo <= n <= hi:
            return b
    return None


def _idx(dn: str, cls: str) -> Optional[int]:
    m = re.search(r"/%s-(\d+)(?:/|$)" % cls, dn)
    return int(m.group(1)) if m else None


def _active_plan(snapshot) -> str:
    """'/NP-1135/' — the plan the runtime radios point to."""
    for dn, v in snapshot.by_class("RMOD_R"):
        m = re.search(r"(/NP-\d+/)", str(v.get("configDN", "")))
        if _RV in dn and m:
            return m.group(1)
    return ""


def radio_rows(snapshot) -> List[Dict[str, object]]:
    """One row per radio × port × band × cell/TRX (a row with only VSWR when
    no cell uses that port/band). Sorted by sector, radio, port, band."""
    plan = _active_plan(snapshot)

    # radios: runtime number, product, serial; local <-> runtime by serial
    radios: Dict[int, dict] = {}
    by_serial: Dict[str, int] = {}
    for dn, v in snapshot.by_class("RMOD_R"):
        k = _idx(dn, "RMOD_R")
        if _RV not in dn or k is None:
            continue
        serial = str(v.get("serialNumber") or v.get("chassisSerialNumber") or "")
        radios[k] = {"product": str(v.get("productName") or ""),
                     "serial": serial, "plan": _idx(str(v.get("configDN", "")),
                                                    "RMOD_A") or k}
        by_serial[serial] = k
    local_to_rt: Dict[int, int] = {}
    for dn, v in snapshot.by_class("RMOD_L"):
        k = _idx(dn, "RMOD_L")
        rt = by_serial.get(str(v.get("serialNumber") or ""))
        if k is not None and rt is not None:
            local_to_rt[k] = rt

    # pipe letter -> band, per runtime radio (filters in index order)
    filters: Dict[int, List[Tuple[int, Optional[int]]]] = defaultdict(list)
    for dn, v in snapshot.by_class("FFU_L"):
        rt = local_to_rt.get(_idx(dn, "RMOD_L"))
        ff = _idx(dn, "FF_L")
        if rt is not None and ff is not None:
            filters[rt].append((ff, band_of_freq(v.get("centerFrequencyDownlink"))))
    pipe_band: Dict[Tuple[int, str], Optional[int]] = {}
    for rt, ffs in filters.items():
        order: List[int] = []
        for _ff, b in sorted(ffs):
            if b is not None and b not in order:
                order.append(b)
        for letter, b in zip("abcd", order):
            pipe_band[(rt, letter)] = b

    # VSWR per runtime radio / port / pipe
    vswr: Dict[Tuple[int, int, str], dict] = {}
    for dn, v in snapshot.by_class("VSWR"):
        rt = local_to_rt.get(_idx(dn, "RMOD_L"))
        m = re.match(r"antenna(\d+)([a-z])$", str(v.get("rp1Name", "")))
        if rt is None or not m:
            continue
        val = v.get("vswr")
        vswr[(rt, int(m.group(1)), m.group(2))] = {
            "value": None if val is None or v.get("invalidVswr") else val / 10.0,
            "minor": (v.get("vswrMinorLimit") or 15) / 10.0,
            "major": (v.get("vswrMajorLimit") or 17) / 10.0,
        }

    # cells: name / band
    cell_name, cell_band = {}, {}
    for dn, v in snapshot.by_class("LNCEL_A"):
        if plan and plan not in dn:
            continue
        k = _idx(dn, "LNCEL_A")
        cell_name[("LTE", k)] = str(v.get("cellName") or "LNCEL-%s" % k)
    for dn, v in snapshot.by_class("LNCEL_FDD_A", "LNCEL_TDD_A"):
        if plan and plan not in dn:
            continue
        k = _idx(dn, "LNCEL_A")
        cell_band[("LTE", k)] = band_of_earfcn(v.get("earfcnDL") or v.get("earfcn"))
    for dn, v in snapshot.by_class("LCELC_A"):
        k = _idx(dn, "LCELC_A")
        if v.get("bandNumber") is not None:
            cell_band[("GSM", k)] = int(v["bandNumber"])
        cell_name.setdefault(("GSM", k), "GSM LCELC-%s" % k)

    # plan RX channels -> (plan radio, port)
    chan: Dict[tuple, Tuple[int, int]] = {}
    for dn, v in snapshot.by_class("CHANNEL_A"):
        if plan and plan not in dn or v.get("direction") != "RX":
            continue
        m = re.search(r"/(LCELL|LCELC)_A-(\d+)/CHANNELGROUP_A-(\d+)/"
                      r"CHANNEL_A-(\d+)$", dn)
        a = re.search(r"/RMOD_A-(\d+)/ANTL_A-(\d+)", str(v.get("antlDN", "")))
        if m and a:
            tech = "LTE" if m.group(1) == "LCELL" else "GSM"
            chan[(tech, int(m.group(2)), int(m.group(3)), int(m.group(4)))] = \
                (int(a.group(1)), int(a.group(2)))

    # RX measurements -> (plan radio, port): [(tech, cell, trx, measure, dBm)]
    rx: Dict[Tuple[int, int], list] = defaultdict(list)
    for cls, key, tech_of in (("RTWP_MEASUREMENT", "rtwpValue", "LTE"),
                              ("RSSI_MEASUREMENT", "rssiValue", "GSM")):
        for dn, v in snapshot.by_class(cls):
            m = re.search(r"/(LNBTS|GNBTS)_M-\d+/CELL_M-(\d+)/"
                          r"CHANNEL_GROUP_M-(\d+)/CHANNEL_M-(\d+)/", dn)
            if not m:
                continue
            tech = "LTE" if m.group(1) == "LNBTS" else "GSM"
            cid = int(m.group(2))
            cell, trx = (cid, None) if tech == "LTE" else (cid // 1000, cid % 1000)
            port = chan.get((tech, cell, int(m.group(3)), int(m.group(4))))
            if port is None:
                continue
            raw = v.get(key)
            dbm = None if raw is None else (raw / 10.0 if tech == "LTE"
                                            else float(raw))
            rx[port].append((tech, cell, trx,
                             "RTWP" if tech == "LTE" else "RSSI", dbm))

    # branch imbalance per cell / TRX
    levels: Dict[tuple, List[float]] = defaultdict(list)
    for items in rx.values():
        for tech, cell, trx, _m, dbm in items:
            if dbm is not None:
                levels[(tech, cell, trx)].append(dbm)
    imbalance = {k: round(max(v) - min(v), 1) for k, v in levels.items()
                 if len(v) > 1}

    sectors = _plan_radio_sectors(chan)
    rows: List[Dict[str, object]] = []
    for rt, info in radios.items():
        k = info["plan"]
        ports = sorted({p for (r_, p, _l) in vswr if r_ == rt}
                       | {p for (r_, p) in rx if r_ == k})
        for port in ports:
            letters = sorted({l for (r_, p, l) in vswr if r_ == rt and p == port})
            for letter in letters or ["?"]:
                band = pipe_band.get((rt, letter))
                vs = vswr.get((rt, port, letter), {})
                base = {
                    "Sector": sectors.get(k, ""),
                    "Radio": "RMOD-%d" % rt, "Product": info["product"],
                    "Serial": info["serial"], "Port": "ANT%d" % port,
                    "Band": "B%s" % band if band else "?",
                    "VSWR": vs.get("value"),
                    "VSWR status": vswr_status(vs),
                }
                cells = [x for x in rx.get((k, port), [])
                         if cell_band.get((x[0], x[1])) == band]
                if not cells:
                    rows.append(dict(base, **{"Tech": "", "Cell": "", "TRX": "",
                                              "Measure": "", "RX (dBm)": None,
                                              "Branch imbalance (dB)": None}))
                for tech, cell, trx, meas, dbm in sorted(
                        cells, key=lambda x: (x[0], x[1], x[2] or 0)):
                    rows.append(dict(base, **{
                        "Tech": tech,
                        "Cell": cell_name.get((tech, cell), str(cell)),
                        "TRX": "" if trx is None else trx,
                        "Measure": meas, "RX (dBm)": dbm,
                        "Branch imbalance (dB)": imbalance.get((tech, cell, trx)),
                    }))
    rows.sort(key=lambda r: (r["Sector"] or "S~", int(r["Radio"][5:]),
                             r["Port"], r["Band"], r["Tech"], str(r["Cell"]),
                             str(r["TRX"])))
    return rows


def vswr_status(vs: dict) -> str:
    val = vs.get("value")
    if val is None:
        return ""
    if val >= vs.get("major", 1.7):
        return "Major"
    if val >= vs.get("minor", 1.5):
        return "Minor"
    if val >= VSWR_WARN - 1e-9:
        return "Warning"
    return "OK"


def _plan_radio_sectors(chan) -> Dict[int, str]:
    """Plan radio -> 'S<n>' from the LTE local cell IDs it carries (last
    digit), when they agree."""
    digits: Dict[int, set] = defaultdict(set)
    for (tech, cell, _g, _c), (radio, _p) in chan.items():
        if tech == "LTE" and cell % 10:
            digits[radio].add(cell % 10)
    return {r: "S%d" % next(iter(d)) for r, d in digits.items() if len(d) == 1}


RADIO_COLUMNS = ["Sector", "Radio", "Product", "Serial", "Port", "Band",
                 "VSWR", "VSWR status", "Tech", "Cell", "TRX", "Measure",
                 "RX (dBm)", "Branch imbalance (dB)"]
