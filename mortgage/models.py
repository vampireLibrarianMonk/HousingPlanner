from dataclasses import dataclass

@dataclass(frozen=True)
class MortgageInputs:
    home_price: float
    down_payment_value: float
    down_payment_is_percent: bool
    loan_term_years: int
    annual_interest_rate_pct: float
    start_month: int
    start_year: int

    include_costs: bool

    # Taxes & costs (we keep all internally normalized to monthly dollars)
    property_tax_value: float
    property_tax_is_percent: bool  # if percent, percent of home price per year
    home_insurance_annual: float
    pmi_monthly: float
    hoa_monthly: float
    other_monthly: float