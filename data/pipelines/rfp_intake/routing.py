"""Deterministic routing and evidence slicing for approved departments."""

from __future__ import annotations

from data.pipelines.rfp_intake.contracts import DepartmentId, RFPMetadataDraft


_KEYWORDS: dict[DepartmentId, tuple[str, ...]] = {
    DepartmentId.warehouse: (
        "warehouse", "warehousing", "storage", "inventory", "sku", "fulfillment",
        "almacén", "almacenamiento", "inventario",
    ),
    DepartmentId.lastmile: (
        "last mile", "shipping", "shipment", "carrier", "delivery", "transportista",
        "envío", "envio", "entrega",
    ),
    DepartmentId.reverse: (
        "return", "returns", "reverse logistics", "devolución", "devoluciones",
        "logística inversa", "logistica inversa",
    ),
}


def selected_departments(metadata: RFPMetadataDraft) -> list[DepartmentId]:
    requested = {item.service_id for item in metadata.services_requested if item.requested}
    return [department for department in DepartmentId if department in requested]


def department_extract(markdown: str, department: DepartmentId, *, radius: int = 2) -> str:
    """Return numbered relevant lines, never the unbounded full document."""
    lines = markdown.splitlines()
    selected: set[int] = set()
    keywords = _KEYWORDS[department]
    for index, line in enumerate(lines):
        folded = line.casefold()
        if any(keyword in folded for keyword in keywords):
            start = max(0, index - radius)
            end = min(len(lines), index + radius + 1)
            selected.update(range(start, end))
    if not selected:
        return ""
    rendered = [f"L{index + 1}: {lines[index]}" for index in sorted(selected)]
    return "\n".join(rendered)[:12000]
