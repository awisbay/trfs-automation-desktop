"""
Detect frequency bands from Ericsson RAN node output.

Three detection methods, in order of reliability:

1. **hgetc commands (primary, recommended)**: Run two moshell commands on each
   node to get authoritative band/frequency information directly from the node's
   MO (Managed Object) tree:
     - ``hgetc nrcelldu bandListManual`` — lists NR bands (band number per NRCellDU)
     - ``hgetc ^eutrancell[FT]DD= freqBand$`` — lists LTE cells with their freqBand number

   This is run **before** sdir and is the most reliable method.

2. **Cell prefix detection (secondary)**: Parse sdir output for Ericsson cell DN
   prefixes (Y-, F-, P-, etc.) that identify bands.

3. **RRU name detection (tertiary)**: Parse RRU names from sdir to supplement
   detection when cell prefixes are insufficient.

The mapping from Ericsson band numbers to TRFS band keys::

    Band Number  ->  TRFS Key
    1            ->  L2100
    3            ->  L1800
    8 / 0        ->  L900      (Band 8 = 900MHz FDD, Band 0 = 900MHz FDD lower)
    28           ->  L700      (LTE) or NR700 (if NRCellDU)
    40           ->  L2300
    41           ->  L2600     (LTE TDD) or NR2600 (if NRCellDU)
    n28          ->  NR700
    n41          ->  NR2600

Typical usage (hgetc method)::

    result = detect_bands_from_hgetc(lte_output, nr_output)
    print(result.detected_bands)  # ['L1800', 'L2100', 'L2600', 'NR2600']

Multi-node flow::

    node_bands = {}
    for node_name in nodes:
        lte_text, nr_text = run_band_detection_commands(node_name)
        result = detect_bands_from_hgetc(lte_text, nr_text)
        node_bands[node_name] = result.detected_bands
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List


# Ericsson cell DN prefix -> band key (authoritative mapping)
# In Ericsson naming: EUtranCellFDD=SITECODE[X]-CELLID
# where X is the prefix letter that identifies the frequency band.
CELL_PREFIX_TO_BAND: Dict[str, str] = {
    "Y": "L900",
    "L": "L700",
    "F": "L1800",
    "W": "L2100",
    "K": "L2300",
    "V": "L2600",
    "P": "NR700",
    "N": "NR2600",
}

# GSM band patterns found in sdir output.
# GSM cells appear as: trx, sec, RU44xx, etc. rather than EUtranCellFDD/NRCellDU.
# The sector line may contain the GSM band indicator.
GSM_BAND_PATTERNS: Dict[str, str] = {
    "G900": "G900",
    "G1800": "G1800",
    "G850": "G850",
    "G1900": "G1900",
}

# ──────────────────────────────────────────────────────────────────────────
# Ericsson freqBand number -> TRFS band key
#
# This is the authoritative mapping used by ``hgetc ^eutrancell[FT]DD= freqBand$``
# and ``hgetc nrcelldu bandListManual``.
# ──────────────────────────────────────────────────────────────────────────
LTE_FREQ_BAND_MAP: Dict[int, str] = {
    1: "L2100",
    3: "L1800",
    7: "L2600",     # Band 7 = 2600MHz FDD (rare, some markets)
    8: "L900",
    0: "L900",      # Band 0 (Ericsson Band 0 = 900MHz FDD low)
    20: "L800",      # Band 20 = 800MHz DD
    28: "L700",
    40: "L2300",
    41: "L2600",     # Band 41 = 2600MHz TDD (LTE)
}

NR_BAND_LIST_MAP: Dict[int, str] = {
    28: "NR700",     # n28 = 700MHz NR
    41: "NR2600",    # n41 = 2600MHz NR
    78: "NR3500",    # n78 = 3500MHz NR (C-band)
    1: "NR2100",     # n1 = 2100MHz NR
    3: "NR1800",     # n3 = 1800MHz NR
    25: "NR1900",    # n25 = 1900MHz NR
}

# RRU single-band mapping (band number string -> LTE band key)
# Used when cell prefixes aren't available in sdir output.
RRU_LTE_BAND_MAP: Dict[str, str] = {
    "B1": "L2100",
    "B3": "L1800",
    "B40": "L2300",
    "B41": "L2600",
}

# AAS (Active Antenna System = NR) RRU mapping
# AAS units are always NR bands.
RRU_NR_BAND_MAP: Dict[str, str] = {
    "B28": "NR700",
    "B41": "NR2600",
    "B78": "NR3500",
}

# Dual-band RRU mapping (RRU name pattern -> list of possible bands)
# B0B28 is a combo RRU supporting Band 0 (900MHz LTE) + Band 28 (700MHz).
# The B28 part is disambiguated by cell prefix: L- = L700, P- = NR700.
RRU_DUAL_BAND_MAP: Dict[str, List[str]] = {
    "B0B28": ["L900", "L700"],
}

# Regex: cell prefix letter followed by dash and cell ID (digits or wildcard)
# Matches Y-121, F-131, P-141, N-131, Y-*, etc.
_CELL_PREFIX_RE = re.compile(r"([YFWLKVPN])-(\d+|\*)")

# Regex: dual-band RRU pattern
_DUAL_BAND_RE = re.compile(r"(B0B28)", re.IGNORECASE)

# Regex: AAS RRU pattern — AAS_B<NUM>_RRU
_AAS_RRU_RE = re.compile(r"AAS_B(\d+)_RRU", re.IGNORECASE)

# Regex: band number from RRU name — B<NUM> followed by _ or RRU
_SINGLE_BAND_RE = re.compile(r"B(\d+)(?:_RRU|_AAS|_RU)", re.IGNORECASE)

# Regex: FRU/RRU row identifier (line in sdir table with semicolons)
_SDIR_DATA_ROW_RE = re.compile(r";\s*[^\s]")

# Regex: GSM indicators in sdir output
_GSM_SECTOR_RE = re.compile(r"(?:SE=|sector=|GSM=|TRX=)", re.IGNORECASE)
_GSM_900_RE = re.compile(r"900", re.IGNORECASE)
_GSM_RU_RE = re.compile(r"RU\d{4}", re.IGNORECASE)

# Regex: band number from GSM cell info (e.g. arfcn 124-174 = GSM900)
# GSM ARFCN ranges: 900=1-124, 1800=512-885
_GSM_ARFCN_900_RE = re.compile(r"arfcn[=\s]*[1-9]\d{0,2}\b", re.IGNORECASE)


@dataclass
class BandDetectionResult:
    """Result of band detection from sdir or hgetc output."""

    detected_bands: List[str] = field(default_factory=list)
    cell_prefixes_found: Dict[str, str] = field(default_factory=dict)
    rru_names: List[str] = field(default_factory=list)
    raw_lines_parsed: int = 0
    raw_output: str = ""
    detection_method: str = ""


def detect_bands_from_hgetc(lte_output: str, nr_output: str) -> BandDetectionResult:
    """
    Detect frequency bands from ``hgetc`` command output (primary method).

    Uses two moshell commands run on each node:
      - ``hgetc ^eutrancell[FT]DD= freqBand$``  -> *lte_output*
      - ``hgetc nrcelldu bandListManual``          -> *nr_output*

    Each LTE cell line looks like::

        EUtranCellFDD=TCFGAMANKILAMTAGUMDDNF-1   ;3

    where the number after the semicolon is the Ericsson band number.
    Each NR cell line looks like::

        NRCellDU=TCFGAMANKILAMTAGUMDDNN-401 ;i[1] = 41

    where the value after ``=`` is the NR band number.

    Args:
        lte_output: Raw output of ``hgetc ^eutrancell[FT]DD= freqBand$``.
        nr_output:  Raw output of ``hgetc nrcelldu bandListManual``.

    Returns:
        :class:`BandDetectionResult` with detected bands and evidence.
    """
    bands: set = set()
    prefixes_found: Dict[str, str] = {}
    lines_parsed = 0
    combined_raw = lte_output + "\n" + nr_output

    # ── Parse LTE freqBand output ──────────────────────────────────────
    # Lines: EUtranCellFDD=SITECODE... ;3   or   EUtranCellTDD=SITECODE... ;41
    for line in lte_output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("MO"):
            continue
        parts = stripped.split(";")
        if len(parts) < 2:
            continue
        cell_dn = parts[0].strip()
        freq_str = parts[-1].strip()

        # Extract the freqBand number
        try:
            freq_band = int(freq_str)
        except ValueError:
            continue

        lines_parsed += 1

        # Map freqBand number to TRFS band key
        if freq_band in LTE_FREQ_BAND_MAP:
            bands.add(LTE_FREQ_BAND_MAP[freq_band])

        # Also extract cell prefix for cross-reference
        prefix_match = _CELL_PREFIX_RE.search(cell_dn)
        if prefix_match:
            prefix = prefix_match.group(1).upper()
            if prefix in CELL_PREFIX_TO_BAND:
                prefixes_found[prefix] = CELL_PREFIX_TO_BAND[prefix]

    # ── Parse NR bandListManual output ─────────────────────────────────
    # Lines: NRCellDU=SITECODE... ;i[1] = 41
    #         or: NRCellDU=SITECODE... ;28
    _NR_BAND_VALUE_RE = re.compile(r"=\s*(\d+)")
    for line in nr_output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("MO"):
            continue

        # Find NRCellDU lines
        if "NRCellDU" not in stripped:
            continue

        parts = stripped.split(";")
        if len(parts) < 2:
            continue

        cell_dn = parts[0].strip()
        value_part = ";".join(parts[1:]).strip()

        # Extract band number: could be "i[1] = 41" or just "28"
        nr_match = _NR_BAND_VALUE_RE.search(value_part)
        if nr_match:
            nr_band_num = int(nr_match.group(1))
            lines_parsed += 1
            if nr_band_num in NR_BAND_LIST_MAP:
                bands.add(NR_BAND_LIST_MAP[nr_band_num])

        # Also try direct integer (e.g., just ";28")
        try:
            direct = int(value_part.strip())
            if direct in NR_BAND_LIST_MAP:
                lines_parsed += 1
                bands.add(NR_BAND_LIST_MAP[direct])
        except ValueError:
            pass

        # Extract cell prefix
        prefix_match = _CELL_PREFIX_RE.search(cell_dn)
        if prefix_match:
            prefix = prefix_match.group(1).upper()
            if prefix in CELL_PREFIX_TO_BAND:
                prefixes_found[prefix] = CELL_PREFIX_TO_BAND[prefix]

    # ── Disambiguate B41: LTE Band 41 vs NR n41 ────────────────────────
    # If freqBand=41 appears in LTE output and n41 in NR output, both
    # L2600 and NR2600 should be present. The hgetc commands naturally
    # separate them (EUtranCellTDD= for LTE, NRCellDU= for NR).

    # ── Disambiguate B28: LTE Band 28 vs NR n28 ────────────────────────
    # Same logic: FDD+28 = L700, NRCellDU+28 = NR700.

    detected = sorted(bands, key=_band_sort_key)

    return BandDetectionResult(
        detected_bands=detected,
        cell_prefixes_found=prefixes_found,
        rru_names=[],
        raw_lines_parsed=lines_parsed,
        raw_output=combined_raw,
        detection_method="hgetc",
    )


def detect_bands_from_sdir(sdir_output: str) -> BandDetectionResult:
    """
    Parse sdir output and detect which frequency bands are present on a node.

    Uses cell prefix detection (primary, authoritative) and RRU name patterns
    (secondary, supplementary). Cell prefixes in Ericsson naming uniquely
    identify bands: Y- = L900, F- = L1800, P- = NR700, etc.

    When a dual-band RRU like B0B28 appears without a disambiguating cell
    prefix for the B28 part (L- vs P-), both L700 and NR700 are added as
    candidates. The TRFS commands file then determines which sections exist.

    Args:
        sdir_output: Raw ``sdir`` command output from an Ericsson node.

    Returns:
        :class:`BandDetectionResult` with detected bands and evidence.
    """
    bands: set = set()
    prefixes_found: Dict[str, str] = {}
    rru_names: List[str] = []
    lines_parsed = 0

    for line in sdir_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip header lines
        if "FRU" in stripped and "VSWR" in stripped:
            continue
        # Skip pure separator lines
        if len(stripped) >= 10 and set(stripped) <= {"=", "-", " "}:
            continue

        parts = stripped.split(";")
        if len(parts) < 3:
            continue

        lines_parsed += 1
        fru_name = parts[0].strip()

        # ---- Primary detection: cell prefix patterns in any column ----
        for match in _CELL_PREFIX_RE.finditer(stripped):
            prefix = match.group(1).upper()
            if prefix in CELL_PREFIX_TO_BAND:
                band_key = CELL_PREFIX_TO_BAND[prefix]
                bands.add(band_key)
                prefixes_found[prefix] = band_key

        # ---- Collect RRU names for secondary detection ----
        if _is_rru_row(fru_name):
            rru_names.append(fru_name)

        # ---- GSM detection: look for GSM-specific indicators ----
        # GSM rows in sdir contain 'trx', 'sec', or 'GSM=' in sector info
        upper_line = stripped.upper()
        if not any(prefix in CELL_PREFIX_TO_BAND for prefix in _CELL_PREFIX_RE.findall(upper_line)):
            # No LTE/NR cell prefix found on this line — check for GSM
            if _GSM_SECTOR_RE.search(upper_line):
                # Found a GSM sector indicator. Default to G900 unless
                # the line explicitly mentions another GSM band.
                if "G1800" in upper_line or "1800" in upper_line:
                    bands.add("G1800")
                elif "G850" in upper_line or "850" in upper_line:
                    bands.add("G850")
                elif "G1900" in upper_line or "1900" in upper_line:
                    bands.add("G1900")
                else:
                    bands.add("G900")
                    prefixes_found["GSM"] = "G900"

    # ---- Secondary detection: RRU name patterns ----
    # Only used if cell prefix detection found nothing (unlikely in real
    # sdir output) or to add bands that cell prefixes may have missed.
    _detect_from_rru_names(rru_names, bands, prefixes_found)

    # ---- Disambiguate B28: if P- prefix found, replace L700 with NR700 ----
    # B28 can be either L700 or NR700. If P- is found, it's NR700.
    # If L- is found, it's L700. If neither, both are candidates.
    if "P" in prefixes_found and "L700" in bands and "NR700" not in bands:
        # Cell prefix P- confirms NR700, not L700
        pass  # Both may coexist on a node with B0B28 RRU
    if "L" in prefixes_found and "NR700" in bands and "L700" not in bands:
        # Cell prefix L- confirms L700, not NR700
        pass

    # ---- Disambiguate B41: if N- prefix found, ensure NR2600 ----
    # B41 can be L2600 or NR2600. AAS prefix means NR.
    # Cell prefix N- confirms NR2600, V- confirms L2600.

    detected = sorted(bands, key=_band_sort_key)

    return BandDetectionResult(
        detected_bands=detected,
        cell_prefixes_found=prefixes_found,
        rru_names=rru_names,
        raw_lines_parsed=lines_parsed,
        raw_output=sdir_output,
        detection_method="sdir",
    )


def _is_rru_row(fru_name: str) -> bool:
    """Check if a FRU name looks like an RRU/AAS row (not a sub-row or header)."""
    if not fru_name or fru_name.startswith("fru_"):
        return False
    upper = fru_name.upper()
    return any(tok in upper for tok in ("_RRU", "AAS_", "_RU"))


def _detect_from_rru_names(
    rru_names: List[str], bands: set, prefixes_found: Dict[str, str]
) -> None:
    """Supplement band detection using RRU name patterns."""
    for rru_name in rru_names:
        is_aas = rru_name.upper().startswith("AAS_")

        # --- Dual-band RRUs ---
        dual_match = _DUAL_BAND_RE.search(rru_name)
        if dual_match:
            dual_key = dual_match.group(1).upper()
            if dual_key in RRU_DUAL_BAND_MAP:
                for band in RRU_DUAL_BAND_MAP[dual_key]:
                    bands.add(band)

        # --- AAS (NR) RRUs ---
        if is_aas:
            aas_match = _AAS_RRU_RE.search(rru_name)
            if aas_match:
                band_num = f"B{aas_match.group(1)}"
                if band_num in RRU_NR_BAND_MAP:
                    bands.add(RRU_NR_BAND_MAP[band_num])

        # --- Single-band LTE RRUs ---
        if not is_aas:
            # Check known LTE band patterns
            for band_key, lte_band in RRU_LTE_BAND_MAP.items():
                if band_key.upper() in rru_name.upper():
                    # Don't add L2600 if NR2600 was already detected via prefix N-
                    if lte_band == "L2600" and "NR2600" in prefixes_found.values():
                        continue
                    # Don't add L700 if NR700 was detected via prefix P-
                    if lte_band == "L700" and "NR700" in prefixes_found.values():
                        continue
                    bands.add(lte_band)

            # B28 without AAS context: default to L700 unless P- prefix found
            if "B28" in rru_name.upper() and "B0" not in rru_name.upper():
                # Standalone B28 RRU (not B0B28 combo)
                if "NR700" not in prefixes_found.values():
                    bands.add("L700")
                else:
                    bands.add("NR700")


def _band_sort_key(band: str) -> tuple:
    """Sort key: LTE bands first (by number), then NR bands (by number)."""
    if band.startswith("L"):
        num = band[1:]
        return (0, int(num) if num.isdigit() else 9999)
    elif band.startswith("NR"):
        num = band[2:]
        return (1, int(num) if num.isdigit() else 9999)
    elif band.startswith("G"):
        num = band[1:]
        return (2, int(num) if num.isdigit() else 9999)
    return (3, 0)


def get_node_bands_from_sdir_map(
    sdir_map: Dict[str, str], all_command_bands: List[str]
) -> Dict[str, List[str]]:
    """
    Given a mapping of node_name -> raw sdir output, detect bands for each
    node and intersect with the bands available in the commands file.

    Only returns bands that are both detected on the node AND have a
    corresponding section in the TRFS commands file.

    Args:
        sdir_map: Dict mapping node_name to raw sdir output string.
        all_command_bands: List of band keys found in TRFS commands.txt.

    Returns:
        Dict mapping node_name to list of band keys to process.
    """
    command_set = set(all_command_bands)
    result: Dict[str, List[str]] = {}

    for node_name, sdir_text in sdir_map.items():
        detection = detect_bands_from_sdir(sdir_text)
        # Only include bands that have commands defined
        node_bands = [b for b in detection.detected_bands if b in command_set]

        # If detection found nothing, fall back to all command bands
        # (preserves backward compatibility for nodes with unusual sdir format)
        if not node_bands and not detection.cell_prefixes_found:
            node_bands = list(command_set)

        result[node_name] = node_bands

    return result