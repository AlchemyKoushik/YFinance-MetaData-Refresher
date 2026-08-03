from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable


REGIONS: tuple[str, ...] = (
    "Africa",
    "Asia",
    "Europe",
    "North America",
    "South America",
    "Oceania",
)


def _normalize_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.strip().lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _display_country(label: str) -> str:
    return label.strip()


def _region_aliases(region: str, countries: Iterable[tuple[str, tuple[str, ...]]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for country, aliases in countries:
        for label in (country, *aliases):
            mapping[_normalize_key(label)] = region
    return mapping


_COUNTRY_GROUPS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "Africa": (
        ("Algeria", ()),
        ("Angola", ()),
        ("Benin", ()),
        ("Botswana", ()),
        ("Burkina Faso", ()),
        ("Burundi", ()),
        ("Cabo Verde", ("Cape Verde",)),
        ("Cameroon", ()),
        ("Central African Republic", ()),
        ("Chad", ()),
        ("Comoros", ()),
        ("Cote d'Ivoire", ("Cote d Ivoire", "Cote D'Ivoire", "Cote D Ivoire", "Côte d'Ivoire", "Ivory Coast")),
        ("Djibouti", ()),
        ("Egypt", ()),
        ("Equatorial Guinea", ()),
        ("Eritrea", ()),
        ("Eswatini", ("Swaziland",)),
        ("Ethiopia", ()),
        ("Gabon", ()),
        ("Gambia", ("The Gambia",)),
        ("Ghana", ()),
        ("Guinea", ()),
        ("Guinea-Bissau", ()),
        ("Kenya", ()),
        ("Lesotho", ()),
        ("Liberia", ()),
        ("Libya", ()),
        ("Madagascar", ()),
        ("Malawi", ()),
        ("Mali", ()),
        ("Mauritania", ()),
        ("Mauritius", ()),
        ("Morocco", ()),
        ("Mozambique", ()),
        ("Namibia", ()),
        ("Niger", ()),
        ("Nigeria", ()),
        ("Rwanda", ()),
        ("Sao Tome and Principe", ("Sao Tome e Principe", "Sao Tome and Principe", "São Tomé and Príncipe")),
        ("Senegal", ()),
        ("Seychelles", ()),
        ("Sierra Leone", ()),
        ("Somalia", ()),
        ("South Africa", ()),
        ("South Sudan", ()),
        ("Sudan", ()),
        ("Tanzania", ("United Republic of Tanzania",)),
        ("Togo", ()),
        ("Tunisia", ()),
        ("Uganda", ()),
        ("Zambia", ()),
        ("Zimbabwe", ()),
        ("Democratic Republic of the Congo", ("Congo, Democratic Republic of the", "DR Congo", "Democratic Republic Congo")),
        ("Republic of the Congo", ("Congo, Republic of the", "Congo Brazzaville")),
        ("Western Sahara", ()),
    ),
    "Asia": (
        ("Afghanistan", ()),
        ("Armenia", ()),
        ("Azerbaijan", ()),
        ("Bahrain", ()),
        ("Bangladesh", ()),
        ("Bhutan", ()),
        ("Brunei", ("Brunei Darussalam",)),
        ("Cambodia", ()),
        ("China", ()),
        ("Georgia", ()),
        ("Hong Kong", ("Hong Kong SAR",)),
        ("India", ()),
        ("Indonesia", ()),
        ("Iran", ("Islamic Republic of Iran",)),
        ("Iraq", ()),
        ("Israel", ()),
        ("Japan", ()),
        ("Jordan", ()),
        ("Kazakhstan", ()),
        ("Kuwait", ()),
        ("Kyrgyzstan", ()),
        ("Laos", ("Lao PDR", "Lao People's Democratic Republic")),
        ("Lebanon", ()),
        ("Malaysia", ()),
        ("Maldives", ()),
        ("Mongolia", ()),
        ("Myanmar", ("Burma",)),
        ("Nepal", ()),
        ("North Korea", ("Democratic People's Republic of Korea", "Korea North")),
        ("Oman", ()),
        ("Pakistan", ()),
        ("Palestine", ("State of Palestine", "Palestinian Territories")),
        ("Philippines", ()),
        ("Qatar", ()),
        ("Saudi Arabia", ()),
        ("Singapore", ()),
        ("South Korea", ("Korea, Republic of", "Republic of Korea", "Korea South")),
        ("Sri Lanka", ()),
        ("Syria", ("Syrian Arab Republic",)),
        ("Taiwan", ("Taiwan, Province of China",)),
        ("Tajikistan", ()),
        ("Thailand", ()),
        ("Timor-Leste", ("East Timor",)),
        ("Turkey", ("Turkiye", "Türkiye")),
        ("Turkmenistan", ()),
        ("United Arab Emirates", ("UAE", "U.A.E.")),
        ("Uzbekistan", ()),
        ("Vietnam", ("Viet Nam",)),
        ("Yemen", ()),
        ("Macau", ("Macao",)),
    ),
    "Europe": (
        ("Albania", ()),
        ("Andorra", ()),
        ("Austria", ()),
        ("Belarus", ()),
        ("Belgium", ()),
        ("Bosnia and Herzegovina", ()),
        ("Bulgaria", ()),
        ("Croatia", ()),
        ("Cyprus", ()),
        ("Czechia", ("Czech Republic",)),
        ("Denmark", ()),
        ("Estonia", ()),
        ("Finland", ()),
        ("France", ()),
        ("Germany", ()),
        ("Gibraltar", ()),
        ("Greece", ()),
        ("Guernsey", ()),
        ("Hungary", ()),
        ("Iceland", ()),
        ("Ireland", ()),
        ("Isle of Man", ()),
        ("Italy", ()),
        ("Jersey", ()),
        ("Kosovo", ()),
        ("Latvia", ()),
        ("Liechtenstein", ()),
        ("Lithuania", ()),
        ("Luxembourg", ()),
        ("Malta", ()),
        ("Moldova", ("Republic of Moldova",)),
        ("Monaco", ()),
        ("Montenegro", ()),
        ("Netherlands", ()),
        ("North Macedonia", ("Macedonia", "Republic of North Macedonia")),
        ("Norway", ()),
        ("Poland", ()),
        ("Portugal", ()),
        ("Romania", ()),
        ("Russia", ("Russian Federation",)),
        ("San Marino", ()),
        ("Serbia", ()),
        ("Slovakia", ()),
        ("Slovenia", ()),
        ("Spain", ()),
        ("Sweden", ()),
        ("Switzerland", ()),
        ("Ukraine", ()),
        ("United Kingdom", ("UK", "U.K.", "Great Britain", "Britain", "England")),
        ("Vatican City", ("Holy See",)),
        ("Faroe Islands", ()),
        ("Svalbard and Jan Mayen", ()),
    ),
    "North America": (
        ("Antigua and Barbuda", ()),
        ("Bahamas", ("The Bahamas",)),
        ("Barbados", ()),
        ("Belize", ()),
        ("Bermuda", ()),
        ("British Virgin Islands", ()),
        ("Canada", ()),
        ("Cayman Islands", ()),
        ("Costa Rica", ()),
        ("Cuba", ()),
        ("Curacao", ("Curaçao",)),
        ("Dominica", ()),
        ("Dominican Republic", ()),
        ("El Salvador", ()),
        ("Greenland", ()),
        ("Grenada", ()),
        ("Guadeloupe", ()),
        ("Guatemala", ()),
        ("Haiti", ()),
        ("Honduras", ()),
        ("Jamaica", ()),
        ("Martinique", ()),
        ("Mexico", ()),
        ("Nicaragua", ()),
        ("Panama", ()),
        ("Puerto Rico", ()),
        ("Saint Barthelemy", ("Saint Barthélemy",)),
        ("Saint Kitts and Nevis", ()),
        ("Saint Lucia", ()),
        ("Saint Martin", ()),
        ("Saint Pierre and Miquelon", ()),
        ("Saint Vincent and the Grenadines", ()),
        ("Sint Maarten", ()),
        ("Trinidad and Tobago", ()),
        ("Turks and Caicos Islands", ()),
        ("United States", ("US", "U.S.", "USA", "U.S.A.", "United States of America", "America")),
        ("United States Virgin Islands", ()),
        ("Anguilla", ()),
        ("Aruba", ()),
        ("Bonaire", ("Bonaire, Sint Eustatius and Saba",)),
        ("Sint Eustatius and Saba", ()),
    ),
    "South America": (
        ("Argentina", ()),
        ("Bolivia", ("Bolivia, Plurinational State of",)),
        ("Brazil", ()),
        ("Chile", ()),
        ("Colombia", ()),
        ("Ecuador", ()),
        ("Falkland Islands", ()),
        ("French Guiana", ()),
        ("Guyana", ()),
        ("Paraguay", ()),
        ("Peru", ()),
        ("Suriname", ()),
        ("Uruguay", ()),
        ("Venezuela", ("Venezuela, Bolivarian Republic of",)),
    ),
    "Oceania": (
        ("Australia", ()),
        ("Cook Islands", ()),
        ("Fiji", ()),
        ("French Polynesia", ()),
        ("Guam", ()),
        ("Kiribati", ()),
        ("Marshall Islands", ()),
        ("Micronesia", ("Federated States of Micronesia",)),
        ("Nauru", ()),
        ("New Caledonia", ()),
        ("New Zealand", ()),
        ("Niue", ()),
        ("Norfolk Island", ()),
        ("Northern Mariana Islands", ()),
        ("Palau", ()),
        ("Papua New Guinea", ()),
        ("Pitcairn Islands", ()),
        ("Samoa", ()),
        ("Solomon Islands", ()),
        ("Tokelau", ()),
        ("Tonga", ()),
        ("Tuvalu", ()),
        ("Vanuatu", ()),
        ("Wallis and Futuna", ()),
        ("American Samoa", ()),
        ("Christmas Island", ()),
        ("Cocos (Keeling) Islands", ()),
    ),
}


COUNTRY_TO_REGION: dict[str, str] = {}
COUNTRY_CANONICAL_BY_KEY: dict[str, str] = {}
COUNTRY_LABELS_BY_CANONICAL: dict[str, set[str]] = {}
for region, countries in _COUNTRY_GROUPS.items():
    COUNTRY_TO_REGION.update(_region_aliases(region, countries))
    for country, aliases in countries:
        canonical_key = _normalize_key(country)
        COUNTRY_CANONICAL_BY_KEY[canonical_key] = country
        COUNTRY_LABELS_BY_CANONICAL.setdefault(country, set()).add(country)
        for alias in aliases:
            COUNTRY_CANONICAL_BY_KEY[_normalize_key(alias)] = country
            COUNTRY_LABELS_BY_CANONICAL[country].add(alias)


@dataclass(slots=True)
class RegionNormalizationInput:
    ticker: str
    country: str
    current_region: str


@dataclass(slots=True)
class RegionNormalizationUpdate:
    ticker: str
    country: str
    expected_region: str


@dataclass(slots=True)
class RegionNormalizationSummary:
    scanned: int = 0
    recognized_countries: int = 0
    rows_requiring_update: int = 0
    updated: int = 0
    skipped: int = 0
    unknown_countries: list[str] = field(default_factory=list)
    updated_country_counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class RegionNormalizationPlan:
    summary: RegionNormalizationSummary
    updates: list[RegionNormalizationUpdate]


def expected_region_for_country(country: str | None) -> str | None:
    if country is None:
        return None
    return COUNTRY_TO_REGION.get(_normalize_key(country))


def country_filter_terms(country: str) -> list[str]:
    normalized = _normalize_key(country)
    if not normalized:
        return []
    canonical = COUNTRY_CANONICAL_BY_KEY.get(normalized)
    if canonical is None:
        return [_display_country(country)]
    terms = {label for label in COUNTRY_LABELS_BY_CANONICAL.get(canonical, {canonical}) if _normalize_key(label)}
    if terms:
        return sorted(terms)
    return [_display_country(country)]


def plan_region_normalization(rows: Iterable[RegionNormalizationInput]) -> RegionNormalizationPlan:
    summary = RegionNormalizationSummary()
    updates: list[RegionNormalizationUpdate] = []
    recognized_countries: set[str] = set()
    unknown_countries: list[str] = []
    unknown_seen: set[str] = set()
    same_region_count = 0
    update_counts: dict[str, int] = {}

    for row in rows:
        summary.scanned += 1
        expected_region = expected_region_for_country(row.country)
        if expected_region is None:
            if row.country not in unknown_seen:
                unknown_seen.add(row.country)
                unknown_countries.append(row.country)
            continue

        recognized_countries.add(row.country)
        if row.current_region == expected_region:
            same_region_count += 1
            continue

        updates.append(
            RegionNormalizationUpdate(
                ticker=row.ticker,
                country=row.country,
                expected_region=expected_region,
            )
        )
        update_counts[row.country] = update_counts.get(row.country, 0) + 1

    summary.recognized_countries = len(recognized_countries)
    summary.rows_requiring_update = len(updates)
    summary.updated = len(updates)
    summary.skipped = same_region_count
    summary.unknown_countries = unknown_countries
    summary.updated_country_counts = update_counts
    return RegionNormalizationPlan(summary=summary, updates=updates)
