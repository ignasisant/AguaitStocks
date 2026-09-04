"""Portugal — mais-valias on securities (categoria G do IRS).

Scope: realized gains and losses on securities held by a Portuguese-resident
individual. NOT tax advice — a planning aid. Rules encoded:

* **28% special rate** (art. 72 CIRS) on the annual *saldo* between mais-valias
  and menos-valias. There is no allowance: the first euro of a positive saldo
  is taxed.
* **FIFO matching** (art. 43 n.6 a) CIRS): the securities sold are the ones
  held longest, which is what positions.py does by default.
* **Mandatory aggregation of short-term gains.** Since 2023 (Lei 24-D/2022),
  the part of the saldo from securities held **less than 365 days** must be
  *englobado* — added to your other income and taxed at the marginal rate —
  when total taxable income reaches the top IRS bracket. That is the one place
  where `TaxSettings.other_income` changes a Portuguese bill, and why the
  holding period is split out here at all.
* **Losses** carry forward **five years**, but only if you opt for englobamento
  in the year of the loss (art. 55 n.1 d) CIRS) — a choice this module cannot
  make for you, so it is a note.

Not modelled: the solidarity surcharge (2.5% above €80,000 of taxable income,
5% above €250,000 — a note when aggregation applies), the 50% relief for
micro/small-company shares, the 35% rate for blacklisted jurisdictions, the
non-habitual-resident and ex-residente regimes, and the fact that the AT wants
foreign amounts at its own reference rates rather than the ECB's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax.base import (
    Kpi,
    Note,
    ReportingFlag,
    TaxPeriod,
    TaxSettings,
    open_period,
    sales_in,
)

CODE = "PT"
CURRENCY = "EUR"
# The tax year is the calendar year here.
YEAR_START = (1, 1)

FLAT_RATE = 0.28  # taxa especial, art. 72 CIRS
# Top marginal IRS rate, used when short-term gains must be aggregated.
TOP_MARGINAL_RATE = 0.48
# Where the last IRS bracket starts, by year. Aggregation of short-term gains
# is mandatory from this level of taxable income up.
TOP_BRACKET: dict[int, float] = {
    2023: 78_834.0,
    2024: 81_199.0,
    2025: 83_696.0,
}
BRACKET_YEARS = sorted(TOP_BRACKET)

# "Menos de 365 dias" — the line the aggregation rule draws.
SHORT_TERM_DAYS = 365
CARRYFORWARD_YEARS = 5

# Anexo J: income of foreign source, declarable whatever its size.
ANEXO_J_THRESHOLD = 0.0


def top_bracket_for(year: int) -> float:
    """Where the last bracket starts, falling back to the closest year on file."""
    if year in TOP_BRACKET:
        return TOP_BRACKET[year]
    earlier = [y for y in TOP_BRACKET if y <= year]
    return TOP_BRACKET[max(earlier) if earlier else min(TOP_BRACKET)]


def is_long_term(buy_date: str, sell_date: str) -> bool:
    """True when the security was held 365 days or more.

    Below that it is short-term, and a top-bracket filer must aggregate it.
    """
    held = (date.fromisoformat(sell_date) - date.fromisoformat(buy_date)).days
    return held >= SHORT_TERM_DAYS


def _is_long(sale: RealizedSale) -> bool:
    return is_long_term(sale.buy_date, sale.sell_date)


@dataclass
class PtTaxPeriod(TaxPeriod):
    """One calendar year of mais-valias for anexo G.

    The saldo nets both holding periods together; the split only decides which
    *rate* the remainder pays, and only for a filer in the top bracket.
    """

    short_gain: float = 0.0
    short_loss: float = 0.0
    long_gain: float = 0.0
    long_loss: float = 0.0
    settings: TaxSettings = field(default_factory=TaxSettings)

    # ------------------------------------------------------------- netting
    @property
    def short_net(self) -> float:
        return self.short_gain - self.short_loss

    @property
    def long_net(self) -> float:
        return self.long_gain - self.long_loss

    @property
    def aggregated(self) -> bool:
        """Whether short-term gains have to be englobados this year."""
        return (
            max(0.0, self.settings.other_income)
            >= top_bracket_for(self.year)
            and self._buckets()[0] > 0
        )

    def _buckets(self) -> tuple[float, float]:
        """(short, long) taxable amounts after the saldo nets across both."""
        short, long = self.short_net, self.long_net
        if short < 0:
            long += short
            short = 0.0
        elif long < 0:
            short += long
            long = 0.0
        return max(0.0, short), max(0.0, long)

    @property
    def net_taxable(self) -> float:
        """The saldo — negative when the year is a net loss."""
        return self.short_net + self.long_net

    @property
    def estimated_tax(self) -> float:
        short, long = self._buckets()
        rate = TOP_MARGINAL_RATE if self.aggregated else FLAT_RATE
        return short * rate + long * FLAT_RATE

    @property
    def carryforward_loss(self) -> float:
        """Negative saldo, carried five years — if englobamento was chosen."""
        return max(0.0, -self.net_taxable)

    # ---------------------------------------------------------- UI surface
    def kpis(self) -> list[Kpi]:
        return [
            Kpi("net_taxable", self.net_taxable, "net_taxable_help"),
            Kpi("estimated_tax", self.estimated_tax, "estimated_tax_help"),
            Kpi("short_net", self.short_net, "short_net_help"),
            Kpi("long_net", self.long_net, "long_net_help"),
            Kpi("carryforward_loss", self.carryforward_loss,
                "carryforward_loss_help"),
        ]

    def notes(self) -> list[Note]:
        out: list[Note] = [Note("flat_rate_note")]
        short, _ = self._buckets()
        if self.aggregated:
            out.append(
                Note("aggregation_note", {"short": f"{short:,.0f}"})
            )
            out.append(Note("solidarity_note"))
        elif short:
            out.append(
                Note(
                    "short_term_note",
                    {
                        "short": f"{short:,.0f}",
                        "threshold": f"{top_bracket_for(self.year):,.0f}",
                    },
                )
            )
        if self.carryforward_loss:
            out.append(Note("carryforward_note"))
        if self.year not in TOP_BRACKET:
            out.append(Note("bracket_year_note", {"year": f"{max(BRACKET_YEARS)}"}))
        return out


def fiscal_period(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> PtTaxPeriod:
    """Summarize an ISO date prefix ("YYYY" or "YYYY-MM") for anexo G.

    `buy_dates` is unused: Portugal has no repurchase rule blocking a loss.
    """
    cfg = settings or TaxSettings()
    out = open_period(PtTaxPeriod, CODE, CURRENCY, period, settings=cfg)
    for s in sales_in(period, realized, YEAR_START):
        out.sales.append(s)
        long = _is_long(s)
        gain = s.gain
        if gain >= 0:
            out.realized_gain += gain
            if long:
                out.long_gain += gain
            else:
                out.short_gain += gain
            continue
        loss = -gain
        out.realized_loss += loss
        if long:
            out.long_loss += loss
        else:
            out.short_loss += loss
    return out


def reporting_flags(
    total_foreign_value: float, settings: TaxSettings | None = None
) -> list[ReportingFlag]:
    """Anexo J: income of foreign source, at any amount.

    A Portuguese resident is taxed on worldwide income, so gains at a foreign
    broker belong in anexo J (and the securities in anexo G) however small.
    There is no value threshold and no wealth tax on securities here — which is
    why this is a single flag with nothing to cross.
    """
    held = f"securities held abroad ~{total_foreign_value:,.0f} EUR"
    msg = (
        f"{held}: gains at a foreign broker go in anexo J, with each operation "
        "listed — no Portuguese broker files it for you."
        if total_foreign_value > 0
        else "nothing held abroad: anexo G alone covers the disposals."
    )
    return [
        # Built directly: `flag` reads a threshold as ">=", which at zero would
        # report a duty for a book with nothing abroad.
        ReportingFlag(
            "anexo_j",
            total_foreign_value,
            ANEXO_J_THRESHOLD,
            total_foreign_value > 0,
            msg,
        )
    ]
