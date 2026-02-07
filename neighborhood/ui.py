from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from locations.logic import _get_loc_by_label
from disaster.ui import _render_doorprofit_usage
from .doorprofit import fetch_neighborhood, get_neighborhood_payload


def _format_percent(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _format_number(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _format_currency(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _coerce_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _key_value_rows(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for label, value in mapping.items():
        rows.append({"Metric": str(label), "Value": _coerce_value(value)})
    return rows


def render_neighborhood() -> None:
    with st.expander(
        "Neighborhood Analysis",
        expanded=st.session_state.get("neighborhood_expanded", False),
    ):
        locations = st.session_state.get("map_data", {}).get("locations", [])
        house = _get_loc_by_label(locations, "House")

        if not house:
            st.warning("Add a location labeled **House** to enable neighborhood analysis.")
            return

        if "neighborhood_payload" not in st.session_state:
            try:
                st.session_state["neighborhood_payload"] = fetch_neighborhood(
                    house.get("address") or ""
                )
            except Exception as exc:
                st.session_state["neighborhood_error"] = str(exc)

        error = st.session_state.get("neighborhood_error")
        if error:
            st.error(error)
            return

        raw_payload = st.session_state.get("neighborhood_payload") or {}
        neighborhood = get_neighborhood_payload(raw_payload)

        if not neighborhood:
            st.info("Neighborhood data is unavailable for this location.")
            return

        name = neighborhood.get("name") or ""
        if name:
            st.markdown(f"**Neighborhood:** {name}")

        data_source = neighborhood.get("data_source")
        if data_source:
            st.caption(f"Source: {data_source}")

        demographics = neighborhood.get("demographics") or {}
        if demographics:
            st.markdown("#### Demographics")
            demo_rows = _key_value_rows(
                {
                    "Population": _format_number(demographics.get("population")),
                    "Population Density": _format_number(demographics.get("population_density")),
                    "Population Growth": _format_percent(demographics.get("population_growth_pct")),
                    "Median Age": demographics.get("median_age"),
                    "Male": _format_percent(
                        (demographics.get("gender") or {}).get("male_pct")
                    ),
                    "Female": _format_percent(
                        (demographics.get("gender") or {}).get("female_pct")
                    ),
                    "White": _format_percent(
                        (demographics.get("race") or {}).get("white_pct")
                    ),
                    "Black": _format_percent(
                        (demographics.get("race") or {}).get("black_pct")
                    ),
                    "Asian": _format_percent(
                        (demographics.get("race") or {}).get("asian_pct")
                    ),
                    "Hispanic": _format_percent(
                        (demographics.get("race") or {}).get("hispanic_pct")
                    ),
                }
            )
            st.dataframe(pd.DataFrame(demo_rows), width="stretch", hide_index=True)

        income = neighborhood.get("income") or {}
        if income:
            st.markdown("#### Income")
            income_rows = _key_value_rows(
                {
                    "Median Income": _format_currency(income.get("median")),
                    "Average Income": _format_currency(income.get("average")),
                }
            )
            st.dataframe(pd.DataFrame(income_rows), width="stretch", hide_index=True)

        housing = neighborhood.get("housing") or {}
        if housing:
            st.markdown("#### Housing")
            housing_rows = _key_value_rows(
                {
                    "Median Home Value": _format_currency(housing.get("median_home_value")),
                    "Total Households": _format_number(housing.get("total_households")),
                    "Avg Year Built": housing.get("avg_year_built"),
                    "Owner Occupied": _format_percent(
                        (housing.get("occupancy") or {}).get("owner_pct")
                    ),
                    "Vacant": _format_percent(
                        (housing.get("occupancy") or {}).get("vacant_pct")
                    ),
                }
            )
            st.dataframe(pd.DataFrame(housing_rows), width="stretch", hide_index=True)

        education = neighborhood.get("education") or {}
        if education:
            st.markdown("#### Education")
            education_rows = _key_value_rows(
                {
                    "High School": _format_percent(education.get("high_school_pct")),
                    "Associate": _format_percent(education.get("associates_pct")),
                    "Bachelor's": _format_percent(education.get("bachelors_pct")),
                    "Graduate": _format_percent(education.get("graduate_pct")),
                }
            )
            st.dataframe(pd.DataFrame(education_rows), width="stretch", hide_index=True)

        col_index = neighborhood.get("cost_of_living") or {}
        if col_index:
            st.markdown("#### Cost of Living Index")
            cost_rows = _key_value_rows(
                {
                    "Overall": col_index.get("overall_index"),
                    "Housing": col_index.get("housing"),
                    "Food": col_index.get("food"),
                    "Healthcare": col_index.get("healthcare"),
                    "Transportation": col_index.get("transportation"),
                    "Utilities": col_index.get("utilities"),
                    "Apparel": col_index.get("apparel"),
                    "Education": col_index.get("education"),
                    "Entertainment": col_index.get("entertainment"),
                }
            )
            st.dataframe(pd.DataFrame(cost_rows), width="stretch", hide_index=True)

        rent = neighborhood.get("rent") or {}
        if rent:
            st.markdown("#### Rent")
            rent_rows = _key_value_rows(rent)
            st.dataframe(pd.DataFrame(rent_rows), width="stretch", hide_index=True)

        weather = neighborhood.get("weather") or {}
        if weather:
            st.markdown("#### Weather")
            temps = weather.get("temperatures") or {}
            precip = weather.get("precipitation") or {}
            disaster = weather.get("natural_disaster_risk") or {}

            st.caption(
                "Natural disaster risk uses DoorProfit’s risk indices where 100 equals the national "
                "average. Values above 100 indicate higher-than-average modeled risk and values below "
                "100 indicate lower-than-average risk. The overall index summarizes the per-hazard "
                "indices for earthquake, hail, hurricane, tornado, and wind shown below."
            )

            temp_rows = _key_value_rows(
                {
                    "Annual High": temps.get("annual_high"),
                    "Annual Low": temps.get("annual_low"),
                    "January High": temps.get("january_high"),
                    "January Low": temps.get("january_low"),
                    "April High": temps.get("april_high"),
                    "April Low": temps.get("april_low"),
                    "July High": temps.get("july_high"),
                    "July Low": temps.get("july_low"),
                    "October High": temps.get("october_high"),
                    "October Low": temps.get("october_low"),
                }
            )

            precip_rows = _key_value_rows(
                {
                    "Rain (inches)": precip.get("rain_inches"),
                    "Rain Days/Year": precip.get("rain_days_per_year"),
                    "Snow (inches)": precip.get("snow_inches"),
                    "Snow Days/Year": precip.get("snow_days_per_year"),
                }
            )

            disaster_rows = _key_value_rows(
                {
                    "Overall Risk Index": disaster.get("overall_index"),
                    "Earthquake": disaster.get("earthquake"),
                    "Hail": disaster.get("hail"),
                    "Hurricane": disaster.get("hurricane"),
                    "Tornado": disaster.get("tornado"),
                    "Wind": disaster.get("wind"),
                }
            )

            st.markdown("**Temperatures**")
            st.dataframe(pd.DataFrame(temp_rows), width="stretch", hide_index=True)
            st.markdown("**Precipitation**")
            st.dataframe(pd.DataFrame(precip_rows), width="stretch", hide_index=True)
            st.markdown("**Natural disaster risk**")
            st.dataframe(pd.DataFrame(disaster_rows), width="stretch", hide_index=True)

        _render_doorprofit_usage("DoorProfit API usage (/v1/usage) — Neighborhood")