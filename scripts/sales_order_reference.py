"""Complete sales-line references with conservative, source-aware matching."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Iterator

from scripts.sync_runtime import order_key, source_hash


def sales_product_scope(row: dict[str, Any]) -> str:
    value = str(row.get("PRODH") or "").strip()
    return "in_scope" if not value or value == "00100" else "out_of_scope"


def sales_lines(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    for row in [*(document.get("records") or []), document.get("data") or {}]:
        if not row:
            continue
        digest = source_hash(row)
        if digest in seen:
            continue
        seen.add(digest)
        yield {"AUFNR": document.get("aufnr", ""), **row}


def trusted_sales_lines(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for row in sales_lines(document):
        if sales_product_scope(row) == "in_scope":
            yield row


class SalesReferences:
    def __init__(self, documents: Iterable[dict[str, Any]]) -> None:
        self.rows: list[dict[str, Any]] = []
        self.by_production: dict[str, list[int]] = defaultdict(list)
        self.by_sales: dict[str, list[int]] = defaultdict(list)
        for document in documents:
            lines = list(sales_lines(document))
            if not lines and document.get("aufnr"):
                lines = [{"AUFNR": document["aufnr"]}]
            for row in lines:
                entry = {
                    "production": str(row.get("AUFNR") or "").strip(),
                    "sales": str(row.get("VBELN") or "").strip(),
                    "planned": str(row.get("GSTRS") or (document.get("gstrs_date") if not document.get("records") else "") or "").strip(),
                    "source": document.get("source", ""),
                    "client": str(row.get("MANDT") or {"SG": "800", "KK": "600"}.get(document.get("source"), "")).strip(),
                    "scope": sales_product_scope(row),
                }
                expected_client = {"SG": "800", "KK": "600"}.get(entry["source"])
                if expected_client and entry["client"] != expected_client:
                    entry["scope"] = "pending"
                index = len(self.rows)
                self.rows.append(entry)
                if entry["production"]:
                    self.by_production[order_key(entry["production"])].append(index)
                if entry["sales"]:
                    self.by_sales[order_key(entry["sales"])].append(index)

    @classmethod
    def load(cls, collection: Any) -> "SalesReferences":
        return cls(collection.find({}, {
            "source": 1, "aufnr": 1, "data": 1, "records": 1, "gstrs_date": 1,
        }))

    def candidates(self, *, production: Any = "", sales: Any = "",
                   client: Any = "", source: str = "") -> list[dict[str, Any]]:
        production, sales, client = order_key(production), order_key(sales), str(client or "").strip()
        indexes = self.by_production.get(production, []) if production else self.by_sales.get(sales, [])
        return [
            self.rows[index] for index in indexes
            if (not sales or order_key(self.rows[index]["sales"]) == sales)
            and (not client or self.rows[index]["client"] == client)
            and (not source or self.rows[index]["source"] == source)
        ]

    def resolve(self, **keys: Any) -> str:
        rows = self.candidates(**keys)
        identities = {(row["source"], row["client"], row["production"]) for row in rows}
        scopes = {row["scope"] for row in rows}
        sales_collision = keys.get("sales") and len({row["sales"] for row in rows}) > 1
        if len(identities) != 1 or len(scopes) != 1 or sales_collision:
            return "pending"
        return scopes.pop()

    def unique(self, field: str, **keys: Any) -> str | None:
        if self.resolve(**keys) != "in_scope":
            return None
        values = {row[field] for row in self.candidates(**keys)}
        return next(iter(values)) if len(values) == 1 and "" not in values else None

    def unique_company(self, field: str, **keys: Any) -> str | None:
        rows = self.candidates(**keys)
        identities = {(row["source"], row["client"], row["production"]) for row in rows}
        values = {row[field] for row in rows}
        return next(iter(values)) if len(identities) == 1 and len(values) == 1 and "" not in values else None

    def row_scope(self, row: dict[str, Any]) -> str:
        return self.resolve(
            production=row.get("AUFNR") or row.get("AUFNR_1") or row.get("AUFNR_HEAD"),
            sales=row.get("VBELN") or row.get("VBELN_EX") or row.get("KDAUF"),
            client=row.get("MANDT"),
        )

    def company_scope(self, row: dict[str, Any]) -> str:
        """Classify by existence in the sales collection, independent of row PRODH."""
        production = order_key(row.get("AUFNR") or row.get("AUFNR_1") or row.get("AUFNR_HEAD"))
        sales = order_key(row.get("VBELN") or row.get("VBELN_EX") or row.get("KDAUF"))
        return "in_scope" if (production and production in self.by_production) or (sales and sales in self.by_sales) else "pending"


def host_scopes(
    collection: Any, refs: SalesReferences, *,
    orders: dict[tuple[str, str], set[tuple[str, str]]] | None = None,
) -> dict[tuple[str, str], set[str]]:
    """Collect host scope and, when requested, verified order pairs in one read."""
    result: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in collection.find({}, {
        "PCODE": 1, "MANDT": 1, "AUFNR": 1, "KDAUF": 1, "_prodh_scope": 1,
    }):
        host = str(row.get("PCODE") or "").strip()
        if host:
            key = (str(row.get("MANDT") or "").strip(), host)
            scope = refs.company_scope(row)
            result[key].add(scope)
            if orders is not None and scope == "in_scope" and row.get("AUFNR"):
                matches = refs.candidates(production=row["AUFNR"], sales=row.get("KDAUF"), client=row.get("MANDT"))
                identities = {(item["source"], item["client"], item["production"]) for item in matches}
                if len(identities) == 1:
                    orders.setdefault(key, set()).add((order_key(row["AUFNR"]), order_key(row.get("KDAUF"))))
    return result
