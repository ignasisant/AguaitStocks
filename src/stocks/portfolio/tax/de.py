"""Germany — Abgeltungsteuer on capital income from securities.

Scope: realized gains/losses on securities for a private investor holding them
outside a business. NOT tax advice — a planning aid. Rules encoded:

* **Flat rate.** 25% Abgeltungsteuer (§32d EStG), plus the 5.5% solidarity
  surcharge *on the tax* (25% × 1.055 = 26.375%), plus church tax at 8% or 9%
  of the tax where the filer pays it. Church tax is deductible, which §32d(1)
  settles inside the assessment (`e / (4 + k)`), so the all-in rate is 27.82%
  at 8% and 27.99% at 9% — not 25% × 1.135/1.145.
* **FIFO.** §20(4) S.7 EStG matches the oldest lots first, which is what
  positions.py already does — no matching mode needed, and no wash-sale rule
  exists here, so a repurchase never blocks a loss.
* **Two loss circles (§20(6) EStG).** Losses on *shares* may only be set
  against gains on shares; everything else (funds, certificates, bonds) offsets
  capital income generally — including share gains. Both carry forward
  indefinitely, and the two carryforwards stay separate.
* **Teilfreistellung (§20 InvStG).** 30% of an equity fund's gain is exempt —
  and so is 30% of its loss, which is the half people forget. Applied to the
  tickers the caller classified as funds (`TaxSettings.fund_tickers`); with no
  classification the figures are computed without it and say so.
* **Sparer-Pauschbetrag.** €1,000 a year, €2,000 for a joint return, against
  the net positive capital income.

Not modelled: the Vorabpauschale on accumulating funds (it needs each fund's
year-end NAV and the Basiszins, neither of which is in a broker statement), the
Günstigerprüfung against the progressive scale, and any Werbungskosten beyond
the commissions already in the lots. Rates are the 2025 figures.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax.base import (
    Kpi,
    Note,
    ReportingFlag,
    TaxPeriod,
    TaxSettings,
    covers,
    flag,
)

CODE = "DE"
CURRENCY = "EUR"
# The tax year is the calendar year here.
YEAR_START = (1, 1)

FILING_STATUSES = ("single", "joint")
DEFAULT_STATUS = "single"

ABGELTUNGSTEUER = 0.25
SOLI_SURCHARGE = 0.055  # of the tax, not of the income
CHURCH_TAX_RATES = (0.0, 0.08, 0.09)  # 8% in BY/BW, 9% elsewhere

# Sparer-Pauschbetrag (§20(9) EStG), per year.
ALLOWANCE = {"single": 1_000.0, "joint": 2_000.0}

# Equity-fund partial exemption (§20 InvStG): applies to gains AND losses.
TEILFREISTELLUNG = 0.30

# Any position held at a non-German broker: no Abgeltungsteuer was withheld at
# source, so the income belongs in Anlage KAP (KAP-INV for funds). A duty at
# any amount, hence a zero threshold rather than a value test.
ANLAGE_KAP_THRESHOLD = 0.0
# AWV/Bundesbank statistical reporting of securities held abroad (§67 AWV).
AWV_THRESHOLD = 5_000_000.0


def _status(settings: TaxSettings | None) -> str:
    s = (settings.filing_status if settings else DEFAULT_STATUS) or DEFAULT_STATUS
    s = s.lower()
    return s if s in FILING_STATUSES else DEFAULT_STATUS


def _church_rate(settings: TaxSettings | None) -> float:
    rate = float(settings.church_tax_rate if settings else 0.0)
    return rate if rate in CHURCH_TAX_RATES else 0.0


def tax_on_capital_income(
    income: float, church_tax_rate: float = 0.0
) -> float:
    """Abgeltungsteuer + solidarity surcharge (+ church tax) on `income`.

    Church tax is itself deductible, and §32d(1) EStG compensates for that in
    the assessment rather than in a separate deduction: the tax is `e / (4 + k)`
    where k is the church-tax rate. At k=0 that is plainly 25%; at k=0.09 it is
    24.45%, which is why the all-in rate lands at ~27.99% and not at
    25% × 1.145. Getting this wrong overstates the bill by ~0.6 points.
    """
    if income <= 0:
        return 0.0
    base = income / (4 + church_tax_rate)
    return base * (1 + SOLI_SURCHARGE + church_tax_rate)


@dataclass
class DeTaxPeriod(TaxPeriod):
    """One year of German capital income from securities.

    The base fields carry the combined totals (so the shared period chart and
    the summary line work unchanged); the fields below carry the split the two
    loss circles need. Fund figures are stored *after* Teilfreistellung —
    that exemption is a property of the income, not of the netting.
    """

    share_gain: float = 0.0
    share_loss: float = 0.0
    # Funds and everything else: one general loss circle.
    general_gain: float = 0.0
    general_loss: float = 0.0
    # How much of the general figures came from funds, and what the partial
    # exemption took off them — surfaced as a note, not a separate bucket.
    fund_exempt: float = 0.0
    funds_classified: bool = True
    settings: TaxSettings = field(default_factory=TaxSettings)

    # ------------------------------------------------------------- netting
    @property
    def share_net(self) -> float:
        """Net result inside the restricted share circle."""
        return self.share_gain - self.share_loss

    @property
    def general_net(self) -> float:
        """Net result of everything that offsets capital income generally."""
        return self.general_gain - self.general_loss

    @property
    def _taxable_before_allowance(self) -> float:
        """Share gains left after the general circle's losses eat into them."""
        share = max(0.0, self.share_net)
        general = self.general_net
        if general >= 0:
            return share + general
        return max(0.0, share + general)  # a general loss may offset shares

    @property
    def allowance(self) -> float:
        """Sparer-Pauschbetrag actually used this year."""
        cap = ALLOWANCE[_status(self.settings)]
        return min(cap, self._taxable_before_allowance)

    @property
    def net_taxable(self) -> float:
        """Capital income after netting and the Sparer-Pauschbetrag."""
        return max(0.0, self._taxable_before_allowance - self.allowance)

    @property
    def estimated_tax(self) -> float:
        return tax_on_capital_income(
            self.net_taxable, _church_rate(self.settings)
        )

    @property
    def share_carryforward(self) -> float:
        """Unused share losses — deductible only against later share gains."""
        return max(0.0, -self.share_net)

    @property
    def general_carryforward(self) -> float:
        """Unused general losses, after they offset any share gains."""
        general_loss = max(0.0, -self.general_net)
        if not general_loss:
            return 0.0
        return max(0.0, general_loss - max(0.0, self.share_net))

    @property
    def carryforward_loss(self) -> float:
        """Both circles' carryforwards. Indefinite, but they stay separate."""
        return self.share_carryforward + self.general_carryforward

    # ----------------------------------------------------------- UI surface
    def kpis(self) -> list[Kpi]:
        return [
            Kpi("net_taxable", self.net_taxable, "net_taxable_help"),
            Kpi("estimated_tax", self.estimated_tax, "estimated_tax_help"),
            Kpi("share_net", self.share_net, "share_net_help"),
            Kpi("general_net", self.general_net, "general_net_help"),
            Kpi("carryforward_loss", self.carryforward_loss,
                "carryforward_loss_help"),
        ]

    def notes(self) -> list[Note]:
        out: list[Note] = []
        if self.allowance:
            out.append(Note("allowance_note", {"used": f"{self.allowance:,.0f}"}))
        if self.fund_exempt:
            out.append(
                Note("teilfreistellung_note", {"exempt": f"{self.fund_exempt:,.0f}"})
            )
        elif not self.funds_classified:
            out.append(Note("funds_unclassified_note"))
        if self.share_carryforward:
            out.append(
                Note(
                    "share_loss_note",
                    {"carry": f"{self.share_carryforward:,.0f}"},
                )
            )
        if _church_rate(self.settings):
            out.append(
                Note(
                    "church_note",
                    {"rate": f"{_church_rate(self.settings) * 100:.0f}"},
                )
            )
        out.append(Note("vorabpauschale_note"))
        return out


def _is_fund(ticker: str, settings: TaxSettings) -> bool:
    return bool(settings.fund_tickers and ticker.upper() in settings.fund_tickers)


def fiscal_period(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> DeTaxPeriod:
    """Summarize an ISO date prefix ("YYYY" or "YYYY-MM") for Anlage KAP.

    `buy_dates` is unused: Germany has no wash-sale rule, so a repurchase
    around a sale never blocks the loss. Kept for the shared signature.
    """
    cfg = settings or TaxSettings()
    out = DeTaxPeriod(
        jurisdiction=CODE,
        currency=CURRENCY,
        year=int(period[:4]),
        period=period,
        settings=cfg,
        funds_classified=cfg.fund_tickers is not None,
    )
    for s in realized:
        if not covers(period, s.sell_date, YEAR_START):
            continue
        out.sales.append(s)
        gain = s.gain
        fund = _is_fund(s.ticker, cfg)
        if fund:
            # 30% of the fund's result — gain or loss — is out of scope.
            out.fund_exempt += abs(gain) * TEILFREISTELLUNG
            gain *= 1 - TEILFREISTELLUNG
        if gain >= 0:
            out.realized_gain += gain
            if fund:
                out.general_gain += gain
            else:
                out.share_gain += gain
            continue
        loss = -gain
        out.realized_loss += loss
        if fund:
            out.general_loss += loss
        else:
            out.share_loss += loss
    return out


def reporting_flags(
    total_foreign_value: float, settings: TaxSettings | None = None
) -> list[ReportingFlag]:
    """What holding these positions abroad adds to the return.

    A German bank withholds the Abgeltungsteuer at source and the income never
    reaches the return; a foreign broker withholds nothing, so the same income
    has to be declared. That is a duty at any amount, which is why the first
    flag has no threshold to clear.
    """
    kap = (
        f"foreign-held securities ~{total_foreign_value:,.0f} EUR: no "
        "Abgeltungsteuer is withheld abroad, so the income goes in Anlage KAP "
        "(KAP-INV for funds)."
        if total_foreign_value > 0
        else "no foreign-held securities: a German broker withholds at source."
    )
    awv = (
        f"foreign securities ~{total_foreign_value:,.0f} EUR ≥ 5,000,000 EUR: "
        "AWV statistical reporting to the Bundesbank may apply."
        if total_foreign_value >= AWV_THRESHOLD
        else f"foreign securities ~{total_foreign_value:,.0f} EUR "
        "< 5,000,000 EUR AWV threshold."
    )
    return [
        # Built directly: `flag` reads a threshold as ">=", which at zero would
        # report a duty for a book with nothing abroad.
        ReportingFlag(
            "anlage_kap",
            total_foreign_value,
            ANLAGE_KAP_THRESHOLD,
            total_foreign_value > 0,
            kap,
        ),
        flag("awv", total_foreign_value, AWV_THRESHOLD, awv),
    ]
