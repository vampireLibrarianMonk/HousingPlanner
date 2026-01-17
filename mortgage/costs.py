from .models import MortgageInputs

def compute_costs_monthly(inputs: MortgageInputs) -> dict:
    """
    Compute monthly home-related costs only.
    Excludes household, vehicle, custom, and income-based expenses by design.
    """

    property_tax_monthly = (
        inputs.home_price * (inputs.property_tax_value / 100.0) / 12.0
        if inputs.property_tax_is_percent
        else inputs.property_tax_value / 12.0
    )

    return {
        "property_tax_monthly": property_tax_monthly,
        "home_insurance_monthly": inputs.home_insurance_annual / 12.0,
        "hoa_monthly": inputs.hoa_monthly,
        "pmi_monthly": inputs.pmi_monthly,
        "other_home_monthly": inputs.other_yearly / 12.0,
    }
