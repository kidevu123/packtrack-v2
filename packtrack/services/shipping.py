"""Express vs sea freight split — port from v1 with no behaviour change."""
from dataclasses import dataclass


@dataclass
class Split:
    express_qty: float
    sea_qty: float
    days_until_stockout: float | None
    days_covered_by_express: int | None
    recommendation: str


def calculate_split(
    total_qty: float,
    current_stock: float,
    daily_usage: float,
    sea_lead_days: int,
    express_lead_days: int,
    buffer_days: int = 14,
) -> Split:
    if daily_usage <= 0:
        return Split(
            express_qty=0,
            sea_qty=total_qty,
            days_until_stockout=None,
            days_covered_by_express=None,
            recommendation="Daily usage is zero — sending everything by sea.",
        )

    days_until_stockout = current_stock / daily_usage
    days_covered_by_express = sea_lead_days - express_lead_days
    units_needed_express = max(0, (sea_lead_days + buffer_days - days_until_stockout) * daily_usage)
    express_qty = min(round(units_needed_express), total_qty)
    sea_qty = total_qty - express_qty

    if express_qty == 0:
        msg = (
            f"You have {days_until_stockout:.0f} days of stock — all {total_qty:g} "
            "units can go sea freight."
        )
    elif express_qty >= total_qty:
        msg = (
            f"Stock critical ({days_until_stockout:.0f} days). Send all "
            f"{total_qty:g} units express."
        )
    else:
        msg = (
            f"Send {express_qty:g} units express to bridge "
            f"{days_covered_by_express} days. Send {sea_qty:g} units by sea."
        )

    return Split(
        express_qty=express_qty,
        sea_qty=sea_qty,
        days_until_stockout=round(days_until_stockout, 1),
        days_covered_by_express=days_covered_by_express,
        recommendation=msg,
    )
