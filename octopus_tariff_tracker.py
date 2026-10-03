#!/usr/bin/env python3
"""
Octopus Energy tariff tracker.

Pulls today's live rates for:
  - Intelligent Octopus Go (fixed) — EV dual-rate electricity tariff
  - Octopus 12M Fixed (dual fuel) — electricity + gas

...for your GSP region, estimates what each would cost you annually and
monthly at your usage (DAY_USAGE_KWH/NIGHT_USAGE_KWH/GAS_USAGE_KWH below),
and appends one dated row per tariff to octopus_tariff_history.csv
(created alongside this script).

No API key needed — Octopus's product/tariff endpoints are public.
Docs: https://docs.octopus.energy/rest/guides/endpoints/

Product codes are NOT hardcoded: Octopus retires and reissues fixed
products every few weeks, so each run searches by name for whichever
version is currently open (available_to == null) and uses that.

Run this daily — cron, Windows Task Scheduler, or a GitHub Actions
schedule (`on: schedule: cron: ...`) all work fine, since this is a
plain HTTPS API call rather than a browser scrape.
"""
import csv
import datetime
import os
import sys
from typing import Optional

import requests

# ---------------------------------------------------------------------
# CONFIG — your postcode (used only to look up your GSP region letter,
# e.g. "H" for Southern England). The outward part is enough, e.g.
# "SW1A" — you don't need the full postcode.
#
# Read from the OCTOPUS_POSTCODE env var first (so it can be set as a
# GitHub Actions secret without being committed to the repo); falls
# back to the literal below for local runs.
# ---------------------------------------------------------------------
POSTCODE = os.environ.get("OCTOPUS_POSTCODE", "CHANGE_ME")

# ---------------------------------------------------------------------
# Your annual usage, used to turn each day's rates into a cost estimate.
# Defaults below are the annualised figures worked out from a meter
# reading (3 Nov 2025 -> 6,032 kWh combined, 13,000 miles at 3.3 mi/kWh)
# in the accompanying spreadsheet. Override via env vars if your usage
# changes, rather than editing this file.
# ---------------------------------------------------------------------
DAY_USAGE_KWH = float(os.environ.get("DAY_USAGE_KWH", "2402"))
NIGHT_USAGE_KWH = float(os.environ.get("NIGHT_USAGE_KWH", "4522"))
GAS_USAGE_KWH = float(os.environ.get("GAS_USAGE_KWH", "9111"))

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "octopus_tariff_history.csv")
BASE = "https://api.octopus.energy/v1"
FIELDNAMES = [
    "date", "option", "product_code", "term_months",
    "elec_day_rate", "elec_night_rate", "elec_standing",
    "gas_rate", "gas_standing", "exit_fee",
    "elec_annual_cost_gbp", "gas_annual_cost_gbp", "total_annual_cost_gbp",
    "elec_monthly_cost_gbp", "gas_monthly_cost_gbp", "total_monthly_cost_gbp",
]


def estimate_costs(row: dict) -> dict:
    """Annual/monthly cost at DAY_USAGE_KWH/NIGHT_USAGE_KWH/GAS_USAGE_KWH,
    given one row's rates, broken out as elec-only / gas-only / total.
    Dual-rate electricity (elec_night_rate present) prices day and night
    usage separately; single-rate electricity (no night rate, e.g. a plain
    dual-fuel fix) prices ALL electricity usage -- day + night combined --
    at the one rate, since there's no cheap overnight window on that
    tariff. Gas-only figures are 0 for an electricity-only tariff."""
    day_rate = row["elec_day_rate"]
    night_rate = row["elec_night_rate"]
    standing = row["elec_standing"]

    if day_rate is None:
        # Gas-only tariff (e.g. 18M Fixed gas) -- no electricity component at all.
        elec_cost = 0.0
    elif night_rate not in ("", None):
        elec_cost = (DAY_USAGE_KWH * day_rate / 100) + (NIGHT_USAGE_KWH * night_rate / 100) + 365 * standing / 100
    else:
        if night_rate is None:
            print(
                f"Warning: {row.get('product_code')} returned a null night rate "
                "(not just 'not applicable') -- pricing all usage at the day rate, "
                "which likely OVERSTATES this tariff's cost. See the raw-shape "
                "debug note in this file before trusting this row.",
                file=sys.stderr,
            )
        elec_cost = (DAY_USAGE_KWH + NIGHT_USAGE_KWH) * day_rate / 100 + 365 * standing / 100

    gas_cost = 0.0
    if row["gas_rate"] not in ("", None):
        gas_cost = (GAS_USAGE_KWH * row["gas_rate"] / 100) + (365 * row["gas_standing"] / 100)
    elif row["gas_rate"] is None:
        print(
            f"Warning: {row.get('product_code')} returned a null gas rate -- "
            "gas cost for this row is 0.0, not a real estimate.",
            file=sys.stderr,
        )

    elec_annual = round(elec_cost, 2)
    gas_annual = round(gas_cost, 2)
    total_annual = round(elec_cost + gas_cost, 2)
    return {
        "elec_annual_cost_gbp": elec_annual,
        "gas_annual_cost_gbp": gas_annual,
        "total_annual_cost_gbp": total_annual,
        "elec_monthly_cost_gbp": round(elec_annual / 12, 2),
        "gas_monthly_cost_gbp": round(gas_annual / 12, 2),
        "total_monthly_cost_gbp": round(total_annual / 12, 2),
    }


def get_region_letter(postcode: str) -> str:
    r = requests.get(f"{BASE}/industry/grid-supply-points/", params={"postcode": postcode}, timeout=15)
    r.raise_for_status()
    results = r.json()["results"]
    if not results:
        raise RuntimeError(f"No GSP region found for postcode '{postcode}'")
    return results[0]["group_id"].lstrip("_")


def find_live_product(name_contains: str, exclude_terms=(), term_months=None, min_days_left=1):
    """Return the currently-open Octopus-brand IMPORT product whose display
    name contains name_contains (case-insensitive), preferring the most
    recently issued version. Excludes any product whose name/code contains
    one of exclude_terms (used to skip special-eligibility variants).

    A product counts as "closed" if available_to is in the past OR within
    min_days_left of now -- Octopus sets available_to to a future timestamp
    shortly before a fixed product actually stops being offered, so
    `available_to is not None` alone lets an about-to-expire product (with
    rates that can be incomplete/null) through right up until the moment
    it closes. term_months, if given, filters to products with that exact
    term (needed when several term lengths share a name, e.g. 12M vs 18M
    Fixed)."""
    r = requests.get(f"{BASE}/products/", params={"is_variable": "false"}, timeout=15)
    r.raise_for_status()
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for p in r.json()["results"]:
        if p.get("brand") != "OCTOPUS_ENERGY":
            continue
        if "OE-FIX" not in p.get("code") and "IOG-" not in p.get("code"):
            continue
        if p.get("direction", "IMPORT") != "IMPORT":
            continue
        if name_contains.lower() not in p["display_name"].lower():
            continue
        available_to = p.get("available_to")
        if available_to is not None:
            closes_at = datetime.datetime.fromisoformat(available_to.replace("Z", "+00:00"))
            if (closes_at - now) < datetime.timedelta(days=min_days_left):
                continue
        if term_months is not None and p.get("term") != term_months:
            continue
        if any(t.lower() in p["display_name"].lower() or t.lower() in p["code"].lower() for t in exclude_terms):
            continue
        candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p["available_from"], reverse=True)
    return candidates[0]


def get_product_detail(code: str) -> dict:
    r = requests.get(f"{BASE}/products/{code}/", timeout=15)
    r.raise_for_status()
    return r.json()


def extract_dual_register_elec(detail: dict, region: str):
    block = detail.get("dual_register_electricity_tariffs", {}).get(f"_{region}")
    if not block:
        return None
    dd = block["direct_debit_monthly"]
    return {
        "elec_day_rate": dd["day_unit_rate_inc_vat"],
        "elec_night_rate": dd["night_unit_rate_inc_vat"],
        "elec_standing": dd["standing_charge_inc_vat"],
        "exit_fee": round(dd["exit_fees_inc_vat"] / 100, 2),
    }


def extract_four_rate_ev_elec(detail: dict, region: str):
    """Intelligent Octopus Go (and similar smart EV tariffs) don't use a
    classic Economy-7 dual-register meter — Octopus publishes their rates
    under 'four_rate_ev_electricity_tariffs' instead, with day/night rates
    plus separate (often identical) EV-device peak/off-peak rates tied to
    the smart-charging schedule. dual_register/single_register both come
    back empty {} for these products."""
    block = detail.get("four_rate_ev_electricity_tariffs", {}).get(f"_{region}")
    if not block:
        return None
    dd = block["direct_debit_monthly"]
    return {
        "elec_day_rate": dd["day_unit_rate_inc_vat"],
        "elec_night_rate": dd["night_unit_rate_inc_vat"],
        "elec_standing": dd["standing_charge_inc_vat"],
        "exit_fee": round(dd["exit_fees_inc_vat"] / 100, 2),
    }


def extract_ev_tariff_elec(detail: dict, region: str):
    """Try every known shape for an EV/dual-rate electricity tariff, in
    order, since Octopus doesn't use the same key consistently across
    products (or product vintages)."""
    return (
        extract_four_rate_ev_elec(detail, region)
        or extract_dual_register_elec(detail, region)
    )


def extract_single_register_elec(detail: dict, region: str):
    block = detail.get("single_register_electricity_tariffs", {}).get(f"_{region}")
    if not block:
        return None
    dd = block["direct_debit_monthly"]
    return {
        "elec_day_rate": dd["standard_unit_rate_inc_vat"],
        "elec_night_rate": "",
        "elec_standing": dd["standing_charge_inc_vat"],
        "exit_fee": round(dd["exit_fees_inc_vat"] / 100, 2),
    }


def extract_gas(detail: dict, region: str):
    block = detail.get("single_register_gas_tariffs", {}).get(f"_{region}")
    if not block:
        return None
    dd = block["direct_debit_monthly"]
    return {
        "gas_rate": dd["standard_unit_rate_inc_vat"],
        "gas_standing": dd["standing_charge_inc_vat"],
    }


# ---------------------------------------------------------------------
# Which tariffs to track. Add/remove entries here -- nothing else in the
# script needs to change. Each entry:
#   label         - just for log messages
#   name_contains - matched against product display_name (case-insensitive)
#   exclude_terms - skip products whose name/code also contains any of these
#   term_months   - filter to this exact term (None = don't filter)
#   elec_kind     - "ev" (four_rate_ev/dual_register, e.g. IOG),
#                   "single" (single_register, e.g. a plain fix), or
#                   None (electricity-only products aren't expected -
#                   used for gas-only tariffs)
#   wants_gas     - whether to also look up + require a gas rate
# ---------------------------------------------------------------------
TARIFF_CONFIGS = [
    {
        "label": "Intelligent Octopus Go",
        "name_contains": "Intelligent Octopus Go",
        "exclude_terms": ("Saver", "OEV", "Loyal"),
        "term_months": None,
        "elec_kind": "ev",
        "wants_gas": False,
    },
    {
        "label": "Intelligent Octopus Go Loyal",
        "name_contains": "Intelligent Octopus Go 12M Loyal",
        "exclude_terms": (),
        "term_months": None,
        "elec_kind": "ev",
        "wants_gas": False,
    },
    {
        "label": "Octopus 12M Fixed (dual fuel)",
        "name_contains": "Octopus 12M Fixed",
        "exclude_terms": (),
        "term_months": 12,
        "elec_kind": "single",
        "wants_gas": True,
    },
    {
        "label": "Octopus 18M Fixed (gas)",
        "name_contains": "Octopus 18M Fixed",
        "exclude_terms": (),
        "term_months": 18,
        # If this turns out to be a dual-fuel product on your account
        # rather than gas-only, change elec_kind to "single" below.
        "elec_kind": None,
        "wants_gas": True,
    },
]

ELEC_EXTRACTORS = {
    "ev": extract_ev_tariff_elec,
    "single": extract_single_register_elec,
    None: None,
}


def resolve_tariff_row(cfg: dict, region: str, today: str) -> Optional[dict]:
    """Look up one tariff per TARIFF_CONFIGS entry and return a CSV-ready
    row dict, or None (with a warning printed) if it can't be resolved.
    Never raises -- a problem with one tariff must not stop the others."""
    product = find_live_product(
        cfg["name_contains"], exclude_terms=cfg["exclude_terms"], term_months=cfg["term_months"]
    )
    if not product:
        print(f"Warning: no live '{cfg['label']}' product found.", file=sys.stderr)
        return None

    detail = get_product_detail(product["code"])

    elec = {"elec_day_rate": None, "elec_night_rate": "", "elec_standing": None, "exit_fee": None}
    extractor = ELEC_EXTRACTORS[cfg["elec_kind"]]
    if extractor is not None:
        elec = extractor(detail, region)
        if not elec:
            print(f"Warning: no electricity rates found for region {region} on {product['code']} ({cfg['label']})", file=sys.stderr)
            return None

    gas = {"gas_rate": "", "gas_standing": ""}
    if cfg["wants_gas"]:
        gas = extract_gas(detail, region)
        if not gas:
            print(f"Warning: no gas rates found for region {region} on {product['code']} ({cfg['label']})", file=sys.stderr)
            return None

    return {
        "date": today,
        "option": product["display_name"],
        "product_code": product["code"],
        "term_months": product.get("term"),
        **elec,
        **gas,
    }


def main():
    if POSTCODE == "CHANGE_ME":
        sys.exit("Set POSTCODE at the top of this script to your postcode (or outward code) first.")

    today = datetime.date.today().isoformat()
    region = get_region_letter(POSTCODE)
    rows = []

    for cfg in TARIFF_CONFIGS:
        try:
            row = resolve_tariff_row(cfg, region, today)
        except Exception as e:
            print(f"Warning: '{cfg['label']}' failed ({type(e).__name__}: {e}) -- skipping it, continuing with the rest.", file=sys.stderr)
            continue
        if row:
            rows.append(row)

    if not rows:
        sys.exit("No rows to write — see warnings above.")

    for row in rows:
        row.update(estimate_costs(row))

    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)

    print(f"Appended {len(rows)} row(s) for {today} (region {region}) to {CSV_PATH}")


if __name__ == "__main__":
    main()
