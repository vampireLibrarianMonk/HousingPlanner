from .models import MortgageInputs

def compute_costs_monthly(inputs: MortgageInputs, method: str) -> dict:
    """
    Normalize costs to monthly amounts. Key difference between methods is *input cadence*.

    NerdWallet: tax & insurance are yearly; HOA & mortgage insurance are monthly. :contentReference[oaicite:4]{index=4}
    Bankrate: includes taxes/insurance/HOA in the monthly payment view; inputs are editable. :contentReference[oaicite:5]{index=5}
    """
    # Property tax monthly:
    if inputs.property_tax_is_percent:
        # percent of home price per year
        annual_tax = inputs.home_price * (inputs.property_tax_value / 100.0)
        property_tax_monthly = annual_tax / 12.0
    else:
        # dollar amount per year for both methods (we keep UI flexible)
        property_tax_monthly = inputs.property_tax_value / 12.0

    home_insurance_monthly = inputs.home_insurance_annual / 12.0

    # HOA and PMI handling:
    # Both Bankrate and NerdWallet treat HOA and PMI as monthly pass-through costs.
    # Differences between calculators are in input cadence and presentation, not math.
    hoa_monthly = inputs.hoa_monthly
    pmi_monthly = inputs.pmi_monthly

    other_monthly = inputs.other_yearly / 12.0

    return {
        "property_tax_monthly": property_tax_monthly,
        "home_insurance_monthly": home_insurance_monthly,
        "hoa_monthly": hoa_monthly,
        "pmi_monthly": pmi_monthly,
        "other_monthly": other_monthly,
    }
