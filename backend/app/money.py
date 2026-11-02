"""
Deterministic Integer-Paise & Basis-Point Financial Engine.
All monetary calculations use exact integer arithmetic (paise and rational fractions).
Never uses IEEE-754 floating-point math for money.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


def rupees_to_paise(amount_str_or_int: str | int | Decimal) -> int:
    """
    Converts a rupee string or integer (e.g. "560", "560.50", 560) into integer paise (56000, 56050).
    Rejects raw floats to prevent binary floating-point imprecision.
    """
    if isinstance(amount_str_or_int, float):
        raise TypeError(
            "Floating-point values are prohibited for monetary conversion; pass int, str, or Decimal."
        )
    dec = Decimal(str(amount_str_or_int))
    paise = (dec * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(paise)


def paise_to_rupees_str(paise: int) -> str:
    """Formats integer paise (e.g. 56050) into a 2-decimal string ('560.50')."""
    if not isinstance(paise, int):
        raise TypeError(f"Expected integer paise, got {type(paise).__name__}")
    sign = "-" if paise < 0 else ""
    abs_p = abs(paise)
    rupees, rem = divmod(abs_p, 100)
    return f"{sign}{rupees}.{rem:02d}"


def percent_to_bps(rate_str_or_int: str | int | Decimal) -> int:
    """Converts a tax percentage (e.g. 18 or '18.0' or '2.5') into basis points (1800, 250)."""
    if isinstance(rate_str_or_int, float):
        raise TypeError("Floating-point tax rates are prohibited; pass int, str, or Decimal.")
    dec = Decimal(str(rate_str_or_int))
    bps = (dec * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(bps)


def bps_to_percent_str(bps: int) -> str:
    """Formats basis points (e.g. 1800 -> '18.00', 500 -> '5.00')."""
    whole, rem = divmod(abs(bps), 100)
    sign = "-" if bps < 0 else ""
    return f"{sign}{whole}.{rem:02d}"


def round_div_half_up(numerator: int, denominator: int) -> int:
    """
    Exact integer division with half-up rounding for non-negative financial values:
    round(numerator / denominator) using integer math only.
    """
    if denominator <= 0:
        raise ValueError("Denominator must be positive")
    if numerator < 0:
        raise ValueError("Numerator must be non-negative")
    return (numerator * 2 + denominator) // (denominator * 2)


@dataclass(frozen=True)
class LineTaxBreakdown:
    qty_num: int
    qty_den: int
    unit_price_paise: int
    tax_rate_bps: int
    line_subtotal_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    line_tax_paise: int
    line_total_paise: int
    is_inter_state: bool


def calculate_line_financials(
    *,
    qty_num: int,
    qty_den: int = 1,
    unit_price_paise: int,
    tax_rate_bps: int,
    seller_state_code: str = "27",
    buyer_state_code: str = "27",
    tax_regime: str = "GST_INDIA",
) -> LineTaxBreakdown:
    """
    Computes exact integer-paise line subtotal and GST split (CGST + SGST for intra-state,
    IGST for inter-state) using rational quantity (qty_num / qty_den).
    For example: dhai (2.5) = qty_num=5, qty_den=2.
    """
    if qty_num < 0 or qty_den <= 0:
        raise ValueError(f"Invalid rational quantity: {qty_num}/{qty_den}")
    if unit_price_paise < 0 or tax_rate_bps < 0:
        raise ValueError("Unit price and tax rate cannot be negative")

    line_subtotal_paise = round_div_half_up(qty_num * unit_price_paise, qty_den)

    if tax_regime == "NONE" or tax_rate_bps == 0:
        return LineTaxBreakdown(
            qty_num=qty_num,
            qty_den=qty_den,
            unit_price_paise=unit_price_paise,
            tax_rate_bps=tax_rate_bps,
            line_subtotal_paise=line_subtotal_paise,
            cgst_paise=0,
            sgst_paise=0,
            igst_paise=0,
            line_tax_paise=0,
            line_total_paise=line_subtotal_paise,
            is_inter_state=False,
        )

    s_state = (seller_state_code or "27").strip()
    b_state = (buyer_state_code or s_state).strip()
    is_inter_state = bool(s_state and b_state and s_state != b_state)

    if tax_regime == "GST_INDIA" and not is_inter_state:
        # Intra-state: split equally into CGST and SGST in integer paise
        half_bps = tax_rate_bps // 2
        cgst_paise = round_div_half_up(line_subtotal_paise * half_bps, 10_000)
        sgst_paise = round_div_half_up(line_subtotal_paise * (tax_rate_bps - half_bps), 10_000)
        igst_paise = 0
        line_tax_paise = cgst_paise + sgst_paise
    else:
        # Inter-state IGST or single-leg VAT / Sales Tax
        cgst_paise = 0
        sgst_paise = 0
        igst_paise = round_div_half_up(line_subtotal_paise * tax_rate_bps, 10_000)
        line_tax_paise = igst_paise

    return LineTaxBreakdown(
        qty_num=qty_num,
        qty_den=qty_den,
        unit_price_paise=unit_price_paise,
        tax_rate_bps=tax_rate_bps,
        line_subtotal_paise=line_subtotal_paise,
        cgst_paise=cgst_paise,
        sgst_paise=sgst_paise,
        igst_paise=igst_paise,
        line_tax_paise=line_tax_paise,
        line_total_paise=line_subtotal_paise + line_tax_paise,
        is_inter_state=is_inter_state,
    )


@dataclass(frozen=True)
class OrderTotalsBreakdown:
    subtotal_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    tax_amount_paise: int
    grand_total_paise: int


def aggregate_order_financials(lines: list[LineTaxBreakdown]) -> OrderTotalsBreakdown:
    subtotal = sum(line.line_subtotal_paise for line in lines)
    cgst = sum(line.cgst_paise for line in lines)
    sgst = sum(line.sgst_paise for line in lines)
    igst = sum(line.igst_paise for line in lines)
    tax_total = cgst + sgst + igst
    return OrderTotalsBreakdown(
        subtotal_paise=subtotal,
        cgst_paise=cgst,
        sgst_paise=sgst,
        igst_paise=igst,
        tax_amount_paise=tax_total,
        grand_total_paise=subtotal + tax_total,
    )
