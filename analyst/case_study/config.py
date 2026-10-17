"""Explicit source and study scope configuration."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY_VERSION = "canonical-v2"
SOURCE_REPOSITORY_URL = "https://github.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026"
AUDITED_SOURCE_COMMIT = "f6adf2451ac6297c1b749abc131cff8a46020dc9"
MASTER_PATH = ROOT / "data" / "master_data.csv"
SOURCE_URL = (
    "https://raw.githubusercontent.com/phongtdt/"
    "RFID_Data-Analyst_Case-Study-Oct-2026/main/mock-api/defect_records.json"
)
MONTHS = tuple(f"2024-{month:02d}" for month in range(1, 7))
SEVERITIES = ("Minor", "Moderate", "Critical", "Unknown")
DOMAIN_VALUES = {
    "defect_type": ("Cosmetic", "Functional", "Structural"),
    "defect_location": ("Component", "Internal", "Surface"),
    "severity": SEVERITIES[:-1],
    "inspection_method": ("Visual Inspection", "Automated Testing", "Manual Testing"),
}
REQUIRED_EVENT_FIELDS = (
    "defect_id",
    "product_id",
    "defect_type",
    "defect_date",
    "defect_location",
    "severity",
    "inspection_method",
    "repair_cost",
)
REQUIRED_MASTER_FIELDS = (
    "product_id",
    "product_name",
    "category",
    "production_line",
    "monthly_output",
)
