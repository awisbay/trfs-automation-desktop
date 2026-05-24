"""
Parse TRFS commands.txt into structured data organized by band and category.
"""
import logging
import re
from typing import Dict, List

logger = logging.getLogger(__name__)


# Mapping from header text in commands.txt to internal band key
BAND_PATTERNS = {
    "FOR NR700": "NR700",
    "FOR NR2600": "NR2600",
    "FOR G900": "G900",
    "FOR L1800": "L1800",
    "FOR L700": "L700",
    "FOR L2100": "L2100",
    "FOR L900": "L900",
    "FOR L2300": "L2300",
    "FOR L2600": "L2600",
}

# Mapping from section header in commands.txt to sheet/category name
CATEGORY_PATTERNS = {
    "VSWR": "VSWR",
    "CELL": "CELL",
    "NOC LOGS": "NOC_Logs",
    "ALARM": "ALARM",
    "PIM": "PIM",
    "RET": "RET",
}


def parse_commands_file(filepath: str) -> Dict[str, Dict[str, List[str]]]:
    """
    Parse TRFS commands.txt and return structured dict:
    {
        "NR700": {
            "VSWR": ["cmd1", "cmd2"],
            "VSWR_ENM": ["ENM item1"],
            "CELL": ["cmd1"],
            "CELL_ENM": ["ENM CELL M SS"],
            ...
        },
        ...
    }
    """
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    result = {}
    current_band = None
    current_category = None

    for line in lines:
        stripped = line.strip()

        # Skip empty lines and pure asterisk separator lines
        if not stripped or re.match(r"^\*+$", stripped):
            continue

        # Check for band header: *******FOR L900 *************
        band_match = _match_band_header(stripped)
        if band_match:
            current_band = band_match
            if current_band not in result:
                result[current_band] = {}
            current_category = None
            continue

        # Check for category header: *********** VSWR *************
        cat_match = _match_category_header(stripped)
        if cat_match:
            current_category = cat_match
            if current_band and current_category not in result.get(current_band, {}):
                result[current_band][current_category] = []
            continue

        # If we have a band and category, this is a command line
        if current_band and current_category:
            # Check if it's an ENM item (in parentheses)
            enm_match = re.match(r"^\((.+)\)$", stripped)
            if enm_match:
                enm_key = f"{current_category}_ENM"
                if enm_key not in result[current_band]:
                    result[current_band][enm_key] = []
                result[current_band][enm_key].append(enm_match.group(1).strip())
            else:
                # Regular moshell command
                # Skip lines that are just comments about running from another BB
                if stripped.startswith("(") and stripped.endswith(")"):
                    continue
                result[current_band][current_category].append(stripped)

    return result


def _match_band_header(line: str) -> str | None:
    """Check if line is a band header like '*******FOR L900 *************'"""
    # Remove asterisks and whitespace
    cleaned = re.sub(r"\*", "", line).strip()
    for pattern, band_key in BAND_PATTERNS.items():
        if pattern in cleaned.upper():
            return band_key
    return None


def _match_category_header(line: str) -> str | None:
    """Check if line is a category header like '*********** VSWR *************'"""
    cleaned = re.sub(r"\*", "", line).strip()
    if not cleaned:
        return None
    for pattern, cat_key in CATEGORY_PATTERNS.items():
        if cleaned.upper() == pattern:
            return cat_key
    return None


def get_band_display_name(band_key: str) -> str:
    """Convert internal band key to display name for filenames."""
    return band_key


def get_all_bands() -> List[str]:
    """Return list of all band keys in order."""
    return list(BAND_PATTERNS.values())


def get_moshell_categories() -> List[str]:
    """Return list of moshell command categories (non-ENM)."""
    return list(CATEGORY_PATTERNS.values())


def filter_gsm_pim(band: str, commands_by_category: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """Remove PIM category from GSM bands (PIM requires BSC access not yet available).

    Args:
        band: Band key (e.g., "G900")
        commands_by_category: Dict of category -> list of commands

    Returns:
        Filtered dict with PIM removed for GSM bands.
    """
    if not band.upper().startswith("G"):
        return commands_by_category
    filtered = {k: v for k, v in commands_by_category.items() if k != "PIM"}
    if "PIM" in commands_by_category:
        logger.info(f"[{band}] PIM skipped — GSM PIM requires BSC access (not yet available)")
    return filtered
