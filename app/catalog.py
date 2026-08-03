from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from app.services.region_normalizer import expected_region_for_country


@dataclass(slots=True)
class CatalogCompany:
    ticker: str
    company_name: str | None = None
    listing_country: str | None = None
    listing_region: str | None = None
    listing_exchange: str | None = None


class CatalogRepository:
    def __init__(self, catalog_path: Path) -> None:
        self.catalog_path = catalog_path
        self._companies: list[CatalogCompany] | None = None

    def load_companies(self) -> list[CatalogCompany]:
        if self._companies is None:
            self._companies = list(self._extract_companies(self._load_payload()))
        return self._companies

    def _load_payload(self) -> Any:
        if not self.catalog_path.exists():
            raise FileNotFoundError(f"IES catalog is missing at {self.catalog_path}")
        with self.catalog_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _extract_companies(self, payload: Any) -> Iterator[CatalogCompany]:
        seen: set[str] = set()

        def walk(node: Any) -> Iterator[CatalogCompany]:
            if isinstance(node, dict):
                ticker = node.get("ticker")
                if isinstance(ticker, str):
                    normalized = ticker.strip().upper()
                    if normalized and normalized not in seen:
                        seen.add(normalized)
                        listing_country = _first_text(node.get("country"))
                        yield CatalogCompany(
                            ticker=normalized,
                            company_name=_first_text(
                                node.get("company_name"),
                                node.get("companyName"),
                                node.get("name"),
                                node.get("shortName"),
                                node.get("longName"),
                                node.get("displayName"),
                            ),
                            listing_country=listing_country,
                            listing_region=expected_region_for_country(listing_country),
                            listing_exchange=_first_text(node.get("exchange")),
                        )
                for value in node.values():
                    yield from walk(value)
            elif isinstance(node, list):
                for item in node:
                    yield from walk(item)

        yield from walk(payload)


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return None
