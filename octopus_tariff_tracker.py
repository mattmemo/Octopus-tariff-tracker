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

    if night_rate != "":
        elec_cost = (DAY_USAGE_KWH * day_rate / 100) + (NIGHT_USAGE_KWH * night_rate / 100)
    else:
        elec_cost = (DAY_USAGE_KWH + NIGHT_USAGE_KWH) * day_rate / 100
    elec_cost += 365 * standing / 100

    gas_cost = 0.0
    if row["gas_rate"] != "":
        gas_cost = (GAS_USAGE_KWH * row["gas_rate"] / 100) + (365 * row["gas_standing"] / 100)

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


def find_live_product(name_contains: str, exclude_terms=()):
    """Return the currently-open Octopus-brand IMPORT product whose display
    name contains name_contains (case-insensitive), preferring the most
    recently issued version. Excludes any product whose name/code contains
    one of exclude_terms (used to skip special-eligibility variants)."""
    r = requests.get(f"{BASE}/products/", params={"is_variable": "false"}, timeout=15)
    r.raise_for_status()
    candidates = []
    for p in r.json()["results"]:
        if p.get("brand") != "OCTOPUS_ENERGY":
            continue
        if "OE-FIX" not in p.get("code"):
            continue  
        if p.get("direction", "IMPORT") != "IMPORT":
            continue
        if name_contains.lower() not in p["display_name"].lower():
            continue
        if p["available_to"] is not None:
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


def main():
    if POSTCODE == "CHANGE_ME":
        sys.exit("Set POSTCODE at the top of this script to your postcode (or outward code) first.")

    today = datetime.date.today().isoformat()
    region = get_region_letter(POSTCODE)
    rows = []

    # --- Intelligent Octopus Go (fixed) — EV dual-rate electricity only ---
    iog = find_live_product("Intelligent Octopus Go", exclude_terms=("Saver", "OEV"))
    if iog:
        detail = get_product_detail(iog["code"])
        elec = extract_ev_tariff_elec(detail, region)
        if elec:
            rows.append({
                "date": today,
                "option": iog["display_name"],
                "product_code": iog["code"],
                "term_months": iog.get("term"),
                "gas_rate": "", "gas_standing": "",
                **elec,
            })
        else:
            print(f"Warning: no EV-tariff electricity rates found (checked four_rate_ev and dual_register) for region {region} on {iog['code']}", file=sys.stderr)
    else:
        print("Warning: no live 'Intelligent Octopus Go' fixed product found.", file=sys.stderr)

    # --- Octopus 12M Fixed (dual fuel) — electricity + gas ---
    fixed = find_live_product("Octopus 12M Fixed")
    if fixed:
        detail = get_product_detail(fixed["code"])
        elec = extract_single_register_elec(detail, region)
        gas = extract_gas(detail, region)
        if elec and gas:
            rows.append({
                "date": today,
                "option": fixed["display_name"],
                "product_code": fixed["code"],
                "term_months": fixed.get("term"),
                **elec,
                **gas,
            })
        else:
            print(f"Warning: missing elec or gas rates for region {region} on {fixed['code']}", file=sys.stderr)
    else:
        print("Warning: no live 'Octopus 12M Fixed' dual-fuel product found.", file=sys.stderr)

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
