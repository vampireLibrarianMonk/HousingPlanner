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


def amortization_schedule_with_adjustments(
    principal: float,
    annual_rate_pct: float,
    term_years: int,
    payment: float,
    recurring_extra_amount: float = 0.0,
    recurring_frequency_months: int = 1,
    recurring_start_month: int = 1,
    recurring_end_month: int | None = None,
    lump_sum_by_month: dict[int, float] | None = None,
) -> pd.DataFrame:
    """
    Month-by-month amortization schedule aggregated by year, with optional
    recurring and one-time lump-sum principal adjustments.

    Parameters use 1-based month indexes across the loan term:
      - recurring_start_month=1 means first month of loan.
      - recurring_end_month=None means continue through term.
      - lump_sum_by_month keys map a specific month index to a lump sum amount.
    """
    n = term_years * 12
    r = (annual_rate_pct / 100.0) / 12.0

    freq = max(int(recurring_frequency_months), 1)
    start_m = max(int(recurring_start_month), 1)
    end_m = n if recurring_end_month is None else min(max(int(recurring_end_month), start_m), n)
    lump_map = lump_sum_by_month or {}

    rows = []
    bal = round(principal, 2)
    year = 1
    interest_ytd = 0.0
    principal_ytd = 0.0

    for m in range(1, n + 1):
        if bal <= 0:
            break

        interest = round(bal * r, 2)
        principal_paid = round(payment - interest, 2)
        if principal_paid < 0:
            principal_paid = 0.0

        recurring_extra = 0.0
        if recurring_extra_amount > 0 and start_m <= m <= end_m and ((m - start_m) % freq == 0):
            recurring_extra = round(recurring_extra_amount, 2)

        lump_extra = round(float(lump_map.get(m, 0.0)), 2)
        total_principal = principal_paid + recurring_extra + lump_extra

        if total_principal > bal:
            total_principal = round(bal, 2)

        bal = round(bal - total_principal, 2)

        interest_ytd = round(interest_ytd + interest, 2)
        principal_ytd = round(principal_ytd + total_principal, 2)

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


def amortization_totals_with_adjustments(
    principal: float,
    annual_rate_pct: float,
    term_years: int,
    payment: float,
    recurring_extra_amount: float = 0.0,
    recurring_frequency_months: int = 1,
    recurring_start_month: int = 1,
    recurring_end_month: int | None = None,
    lump_sum_by_month: dict[int, float] | None = None,
) -> tuple[float, float, int]:
    """
    Returns (total_interest, total_paid_pi, months_to_payoff) with optional
    recurring and one-time principal adjustments.
    """
    n = term_years * 12
    r = (annual_rate_pct / 100.0) / 12.0

    freq = max(int(recurring_frequency_months), 1)
    start_m = max(int(recurring_start_month), 1)
    end_m = n if recurring_end_month is None else min(max(int(recurring_end_month), start_m), n)
    lump_map = lump_sum_by_month or {}

    bal = round(principal, 2)
    total_interest = 0.0
    total_paid = 0.0
    months_to_payoff = 0

    for m in range(1, n + 1):
        if bal <= 0:
            break

        interest = round(bal * r, 2)
        principal_paid = round(payment - interest, 2)
        if principal_paid < 0:
            principal_paid = 0.0

        recurring_extra = 0.0
        if recurring_extra_amount > 0 and start_m <= m <= end_m and ((m - start_m) % freq == 0):
            recurring_extra = round(recurring_extra_amount, 2)

        lump_extra = round(float(lump_map.get(m, 0.0)), 2)

        total_principal = principal_paid + recurring_extra + lump_extra
        if total_principal > bal:
            total_principal = round(bal, 2)

        payment_effective = round(interest + total_principal, 2)

        bal = round(bal - total_principal, 2)
        total_interest = round(total_interest + interest, 2)
        total_paid = round(total_paid + payment_effective, 2)
        months_to_payoff = m

    return total_interest, total_paid, months_to_payoff



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
    return payoff_date_from_months(start_year, start_month, term_years * 12)


def payoff_date_from_months(start_year: int, start_month: int, total_months: int) -> str:
    # payoff month is start + n-1 months (display only)
    n = max(int(total_months), 1)
    y = start_year
    m = start_month
    m_total = (y * 12 + (m - 1)) + (n - 1)
    y2 = m_total // 12
    m2 = (m_total % 12) + 1
    return date(y2, m2, 1).strftime("%b. %Y")
