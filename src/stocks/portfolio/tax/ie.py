"""Ireland — CGT on shares, and the 41% exit tax on funds.

Scope: disposals by an Irish-resident, ordinarily resident and domiciled
individual. NOT tax advice — a planning aid. Rules encoded:

* **33% CGT** on the net chargeable gain, after the **personal exemption of
  €1,270** (s.601 TCA 1997). The exemption is per person, cannot be
  transferred to a spouse and cannot be carried forward — an unused one is
  simply gone.
* **FIFO matching** (s.580 TCA 1997), which is what positions.py does by
  default.
* **The four-week rule** (s.581): a loss on shares reacquired within four
  weeks *after* the disposal is not available generally — only against a gain
  on the disposal of those reacquired shares. The window is forward-only,
  unlike Spain's two months or the US 30 days either side, so this module
  uses `days_after_window`. The loss is deferred, not lost, and comes back as
  the replacement shares are themselves sold.
* **Funds are a different regime.** Irish and EU-domiciled UCITS (ETFs
  included) sit in gross roll-up: gains are charged at **41% exit tax**, with
  no annual exemption, and a loss on them is **not allowable against anything**
  — plus a deemed disposal every eight years. Applied to the tickers the
  caller classified as funds (`TaxSettings.fund_tickers`); with no
  classification everything is treated as shares and the tab says so.
* **Losses** carry forward indefinitely, against chargeable gains only.

Not modelled: the deemed disposal itself (it needs each fund's NAV on the
eighth anniversary), the four-week rule's *matching* leg (shares bought and
sold inside four weeks are matched against each other before FIFO), whether a
particular ETF is EU/Irish-domiciled — a US-domiciled one is CGT, not exit tax
— development-land rules, and PRSA/PRSI interactions.
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
    days_after_window,
    open_period,
    recovered_losses,
    replacement_dates,
    sales_in,
)

CODE = "IE"
CURRENCY = "EUR"
# The tax year is the calendar year here.
YEAR_START = (1, 1)

CGT_RATE = 0.33
EXIT_TAX_RATE = 0.41  # Irish/EU funds in gross roll-up (s.739 / s.747)
PERSONAL_EXEMPTION = 1_270.0
DEEMED_DISPOSAL_YEARS = 8

# Four weeks *after* the disposal (s.581). Forward-only, and 28 days exactly.
WINDOW = days_after_window(28)


def cgt(net_gain: float) -> float:
    """33% on a positive net chargeable gain (after the exemption)."""
    return max(0.0, net_gain) * CGT_RATE


def exit_tax(fund_gain: float) -> float:
    """41% on a positive fund gain. Fund losses relieve nothing."""
    return max(0.0, fund_gain) * EXIT_TAX_RATE


@dataclass
class IeTaxPeriod(TaxPeriod):
    """One calendar year, split across the two regimes.

    The base fields carry the whole book so the shared period chart still
    shows it, with one wrinkle: `disallowed_loss` holds both the four-week
    restrictions *and* every fund loss, because a fund loss is not allowable
    against anything either. The notes say which is which.
    """

    share_gain: float = 0.0
    share_loss: float = 0.0
    share_disallowed: float = 0.0  # blocked by the four-week rule
    fund_gain: float = 0.0
    fund_loss: float = 0.0
    funds_classified: bool = True
    settings: TaxSettings = field(default_factory=TaxSettings)

    # ------------------------------------------------------------- netting
    @property
    def share_net(self) -> float:
        """Chargeable gains less allowable losses, before the exemption."""
        return self.share_gain - (
            self.share_loss - self.share_disallowed
        ) - self.recovered_loss

    @property
    def exemption(self) -> float:
        """Personal exemption actually used — capped by the net gain."""
        return min(PERSONAL_EXEMPTION, max(0.0, self.share_net))

    @property
    def wasted_exemption(self) -> float:
        """Exemption left unused. It cannot be carried or transferred."""
        return max(0.0, PERSONAL_EXEMPTION - self.exemption)

    @property
    def share_taxable(self) -> float:
        return max(0.0, self.share_net - self.exemption)

    @property
    def fund_net(self) -> float:
        """Fund result. Only the positive half is ever taxed."""
        return self.fund_gain - self.fund_loss

    @property
    def net_taxable(self) -> float:
        """Everything the two rates are applied to this year."""
        return self.share_taxable + max(0.0, self.fund_gain)

    @property
    def estimated_tax(self) -> float:
        return cgt(self.share_taxable) + exit_tax(self.fund_gain)

    @property
    def carryforward_loss(self) -> float:
        """Unused share losses. Fund losses are not allowable at all."""
        return max(0.0, -self.share_net)

    # ---------------------------------------------------------- UI surface
    def kpis(self) -> list[Kpi]:
        return [
            Kpi("net_taxable", self.net_taxable, "net_taxable_help"),
            Kpi("estimated_tax", self.estimated_tax, "estimated_tax_help"),
            Kpi("exemption", self.exemption, "exemption_help"),
            Kpi("fund_net", self.fund_net, "fund_net_help"),
            Kpi("carryforward_loss", self.carryforward_loss,
                "carryforward_loss_help"),
        ]

    def notes(self) -> list[Note]:
        out: list[Note] = []
        if self.exemption:
            out.append(Note("exemption_note", {"used": f"{self.exemption:,.0f}"}))
        if self.wasted_exemption and self.share_gain:
            out.append(
                Note("wasted_exemption_note",
                     {"wasted": f"{self.wasted_exemption:,.0f}"})
            )
        if self.share_disallowed:
            out.append(
                Note("four_week_note",
                     {"deferred": f"{self.share_disallowed:,.0f}"})
            )
        if self.recovered_loss:
            out.append(
                Note("recovered_note", {"recovered": f"{self.recovered_loss:,.0f}"})
            )
        if self.fund_gain:
            out.append(Note("fund_note", {"gain": f"{self.fund_gain:,.0f}"}))
        if self.fund_loss:
            out.append(Note("fund_loss_note", {"loss": f"{self.fund_loss:,.0f}"}))
        elif not self.funds_classified:
            out.append(Note("funds_unclassified_note"))
        if self.carryforward_loss:
            out.append(Note("carryforward_note"))
        out.append(Note("payment_note"))
        return out


def _is_fund(ticker: str, settings: TaxSettings) -> bool:
    return bool(settings.fund_tickers and ticker.upper() in settings.fund_tickers)


def fiscal_period(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> IeTaxPeriod:
    """Summarize an ISO date prefix ("YYYY" or "YYYY-MM") for the CG1/Form 11.

    `buy_dates` drives the four-week rule against reacquisitions in any year.
    The monthly slice is a breakdown of when a result was booked; the exemption
    and the losses settle over the whole year.
    """
    cfg = settings or TaxSettings()
    out = open_period(
        IeTaxPeriod, CODE, CURRENCY, period,
        settings=cfg, funds_classified=cfg.fund_tickers is not None,
    )
    for s in sales_in(period, realized, YEAR_START):
        out.sales.append(s)
        fund = _is_fund(s.ticker, cfg)
        gain = s.gain
        if gain >= 0:
            out.realized_gain += gain
            if fund:
                out.fund_gain += gain
            else:
                out.share_gain += gain
            continue
        loss = -gain
        out.realized_loss += loss
        if fund:
            # No relief whatever: it never offsets a gain, here or later.
            out.fund_loss += loss
            out.disallowed_loss += loss
            continue
        out.share_loss += loss
        if replacement_dates(s, buy_dates.get(s.ticker, []), WINDOW):
            out.share_disallowed += loss
            out.disallowed_loss += loss
    out.recovered_loss = recovered_losses(
        realized,
        period,
        buy_dates,
        WINDOW,
        blocked_filter=lambda s: not _is_fund(s.ticker, cfg),
    )
    return out


def reporting_flags(
    total_foreign_value: float, settings: TaxSettings | None = None
) -> list[ReportingFlag]:
    """None: Ireland has no foreign-asset value threshold to cross.

    Foreign shares are chargeable assets like any other and go on the same
    return; there is no FBAR or Modelo 720 analogue. What does bite is the
    calendar — CGT on disposals from 1 January to 30 November is payable by 15
    December of the *same* year — and that is a period note, not a property of
    what is held.
    """
    return []
