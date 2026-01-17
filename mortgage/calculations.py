from datetime import date
import pandas as pd

def monthly_pi_payment(principal: float, annual_rate_pct: float, term_years: int) -> float:
    """
    Standard fixed-rate amortization payment:
      M = P * [ r(1+r)^n / ((1+r)^n - 1) ]
    where r = annual_rate/12, n = years*12.

    Bankrate explicitly publishes this form and defines r as annual/12. :contentReference[oaicite:3]{index=3}
    """
    if principal <= 0:
        return 0.0
    n = term_years * 12
    r = (annual_rate_pct / 100.0) / 12.0
    if r == 0:
        return principal / n
    num = r * (1 + r) ** n
    den = (1 + r) ** n - 1
    return principal * (num / den)


def amortization_schedule(
    principal: float,
    annual_rate_pct: float,
    term_years: int,
    payment: float,
) -> pd.DataFrame:
    """
    Month-by-month amortization schedule aggregated by year.
    """
    n = term_years * 12
    r = (annual_rate_pct / 100.0) / 12.0

    rows = []
    bal = principal
    year = 1
    interest_ytd = 0.0
    principal_ytd = 0.0

    for m in range(1, n + 1):
        if bal <= 0:
            break

        interest = round(bal * r, 2)
        principal_paid = round(payment - interest, 2)

        if principal_paid > bal:
            principal_paid = bal

        bal = round(bal - principal_paid, 2)

        interest_ytd += interest
        principal_ytd += principal_paid

        if m % 12 == 0 or bal <= 0:
            rows.append({
                "Year": year,
                "Interest": interest_ytd,
                "Principal": principal_ytd,
                "Ending Balance": bal,
            })
            year += 1
            interest_ytd = 0.0
            principal_ytd = 0.0

    return pd.DataFrame(rows)


def amortization_schedule_with_extra(
    principal: float,
    annual_rate_pct: float,
    term_years: int,
    payment: float,
    extra_payment_annual: float,
) -> pd.DataFrame:
    """
    Same as amortization_schedule, but applies an extra annual
    principal payment at the end of each year.
    """
    n = term_years * 12
    r = (annual_rate_pct / 100.0) / 12.0

    rows = []
    bal = principal
    year = 1
    interest_ytd = 0.0
    principal_ytd = 0.0

    for m in range(1, n + 1):
        if bal <= 0:
            break

        interest = round(bal * r, 2)
        principal_paid = round(payment - interest, 2)

        if principal_paid > bal:
            principal_paid = bal

        bal -= principal_paid
        interest_ytd += interest
        principal_ytd += principal_paid

        # Apply extra payment once per year
        if m % 12 == 0:
            bal = max(0.0, bal - extra_payment_annual)

            rows.append({
                "Year": year,
                "Interest": interest_ytd,
                "Principal": principal_ytd + extra_payment_annual,
                "Ending Balance": bal,
            })

            year += 1
            interest_ytd = 0.0
            principal_ytd = 0.0

    return pd.DataFrame(rows)



def amortization_totals(principal: float, annual_rate_pct: float, term_years: int, payment: float) -> tuple[float, float]:
    """
    Compute total interest and total paid (P+I) using a month-by-month schedule with cent rounding.
    This avoids drift and better matches what calculators display.
    """
    n = term_years * 12
    r = (annual_rate_pct / 100.0) / 12.0

    bal = principal
    total_interest = 0.0
    total_paid = 0.0

    for m in range(1, n + 1):
        if bal <= 0:
            break
        interest = round(bal * r, 2)
        principal_paid = round(payment - interest, 2)

        # If we're overpaying in the final month, clamp.
        if principal_paid > bal:
            principal_paid = round(bal, 2)
            payment_effective = round(principal_paid + interest, 2)
        else:
            payment_effective = round(payment, 2)

        bal = round(bal - principal_paid, 2)
        total_interest = round(total_interest + interest, 2)
        total_paid = round(total_paid + payment_effective, 2)

    return total_interest, total_paid


def payoff_date(start_year: int, start_month: int, term_years: int) -> str:
    # payoff month is start + n-1 months (display only)
    n = term_years * 12
    y = start_year
    m = start_month
    m_total = (y * 12 + (m - 1)) + (n - 1)
    y2 = m_total // 12
    m2 = (m_total % 12) + 1
    return date(y2, m2, 1).strftime("%b. %Y")
