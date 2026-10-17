"""Immutable, analysis-ready row types and snapshot lineage."""

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import POLICY_VERSION


@dataclass(frozen=True)
class Product:
    product_id: int
    product_name: str
    category: str
    production_line: str
    monthly_output: int | None


@dataclass(frozen=True)
class Event:
    defect_id: int
    product_id: int
    defect_type: str
    defect_date: date
    defect_location: str
    severity: str
    inspection_method: str
    repair_cost: float | None
    cost_status: str
    source_row: int
    product_name: str = "Unmatched product"
    category: str = "Unknown"
    production_line: str = "Unknown"
    matched_product: bool = False

    def as_record(self) -> dict[str, Any]:
        return {**self.__dict__, "defect_date": self.defect_date.isoformat()}


@dataclass(frozen=True)
class AuditEntry:
    source: str
    source_row: int
    record_id: str
    code: str
    severity: str
    field: str
    raw_value: str
    action: str
    detail: str
    processed_value: str | None = None
    policy_version: str = POLICY_VERSION
    source_rows: tuple[int, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    products: tuple[Product, ...]
    events: tuple[Event, ...]
    audit: tuple[AuditEntry, ...]
    quality: dict[str, Any]
    provenance: dict[str, Any]
    raw_summary: dict[str, Any] = field(default_factory=dict)
    # Immutable JSON strings preserve the fetched values in memory. They are
    # never written as a raw-data cache or loaded from a local JSON source.
    raw_records: tuple[str, ...] = field(default=(), repr=False)
    master_bytes: bytes = field(default=b"", repr=False)
    applied_scenario: str = ""
