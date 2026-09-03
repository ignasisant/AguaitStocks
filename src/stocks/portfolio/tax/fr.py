"""France — plus-values de cession de valeurs mobilières under the PFU.

Scope: realized gains and losses on securities held directly (compte-titres
ordinaire) by a French-resident individual. NOT tax advice — a planning aid.
Rules encoded:

* **Prélèvement forfaitaire unique**, 30% all in: 12.8% income tax plus 17.2%
  prélèvements sociaux (CSG 9.2, CRDS 0.5, prélèvement de solidarité 7.5). The
  split matters — the two halves are assessed and paid differently — so both
  are shown rather than one blended rate.
* **Prix moyen pondéré d'acquisition** (CGI art. 150-0 D, 3): identical
  securities are one holding at one weighted-average cost, recomputed on every
  purchase. That is `build(matching="average")`, and it is why a French cost
  basis is nobody's individual purchase price.
* **Losses** offset gains of the same nature in the year and carry forward
  **10 years** (art. 150-0 D, 11). They can never touch other income.
* **No annual allowance**: the first euro of net gain is taxed.
* Foreign accounts must be declared (formulaire 3916/3916-bis) whatever they
  hold — a duty at any amount, hence a flag with no threshold to cross.

Not modelled: the option for the barème progressif (art. 200 A, 2 — it is a
whole-return, irrevocable choice covering every investment income of the year,
which this app cannot see), the abattement for securities acquired before 2018
that goes with it, the CEHR on high incomes (3%/4%, surfaced as a note), the
PEA and assurance-vie wrappers, and the 500,000 € apport-cession regimes.
"""

from __future__ import annotations

from dataclasses import dataclass

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax.base import (
    Kpi,
    Note,
    ReportingFlag,
    TaxPeriod,
    TaxSettings,
    covers,
)

CODE = "FR"
CURRENCY = "EUR"
# The tax year is the calendar year here.
YEAR_START = (1, 1)

INCOME_TAX_RATE = 0.128  # impôt sur le revenu, taux forfaitaire
SOCIAL_RATE = 0.172  # prélèvements sociaux
PFU_RATE = INCOME_TAX_RATE + SOCIAL_RATE  # 0.30

CARRYFORWARD_YEARS = 10

# Contribution exceptionnelle sur les hauts revenus: 3% above this revenu
# fiscal de référence, 4% above 500.000 €. Not computed — the RFR is a
# whole-household figure — but worth saying when the result is this big.
CEHR_THRESHOLD = 250_000.0

# Déclaration 3916: any account opened, held or closed abroad, at any value.
FORM_3916_THRESHOLD = 0.0


def pfu(net_gain: float) -> float:
    """The 30% flat tax on a positive net gain. 0 on a net loss."""
    return max(0.0, net_gain) * PFU_RATE


@dataclass
class FrTaxPeriod(TaxPeriod):
    """One calendar year of plus-values, taxed at the PFU."""

    @property
    def taxable(self) -> float:
        """Net gain actually taxed — a net loss taxes nothing and carries on."""
        return max(0.0, self.net_taxable)

    @property
    def income_tax(self) -> float:
        return self.taxable * INCOME_TAX_RATE

    @property
    def social_charges(self) -> float:
        return self.taxable * SOCIAL_RATE

    @property
    def estimated_tax(self) -> float:
        return self.income_tax + self.social_charges

    def kpis(self) -> list[Kpi]:
        return [
            Kpi("net_taxable", self.net_taxable, "net_taxable_help"),
            Kpi("estimated_tax", self.estimated_tax, "estimated_tax_help"),
            Kpi("income_tax", self.income_tax, "income_tax_help"),
            Kpi("social_charges", self.social_charges, "social_charges_help"),
            Kpi("carryforward_loss", self.carryforward_loss,
                "carryforward_loss_help"),
        ]

    def notes(self) -> list[Note]:
        out: list[Note] = [Note("pfu_note")]
        if self.carryforward_loss:
            out.append(
                Note("carryforward_note",
                     {"carry": f"{self.carryforward_loss:,.0f}"})
            )
        if self.taxable >= CEHR_THRESHOLD:
            out.append(Note("cehr_note"))
        out.append(Note("pea_note"))
        return out


def fiscal_period(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> FrTaxPeriod:
    """Summarize an ISO date prefix ("YYYY" or "YYYY-MM") for the 2074.

    `buy_dates` is unused: France has no repurchase rule blocking a loss — a
    buy-back simply re-averages the holding, which the replay already did.
    """
    out = FrTaxPeriod(
        jurisdiction=CODE, currency=CURRENCY, year=int(period[:4]), period=period
    )
    for s in realized:
        if not covers(period, s.sell_date, YEAR_START):
            continue
        out.sales.append(s)
        if s.gain >= 0:
            out.realized_gain += s.gain
        else:
            out.realized_loss += -s.gain
    return out


def reporting_flags(
    total_foreign_value: float, settings: TaxSettings | None = None
) -> list[ReportingFlag]:
    """Formulaire 3916: foreign accounts, declared at any value.

    There is no threshold to argue about — a compte-titres held abroad is
    declarable even if empty, and the penalty for omitting it is a fixed fine
    per account per year. Whether your broker counts as "abroad" is the part
    the ledger cannot know, so this stays a flag.
    """
    held = f"securities held abroad ~{total_foreign_value:,.0f} EUR"
    msg = (
        f"{held}: a foreign account is declarable on formulaire 3916/3916-bis "
        "whatever it holds, and the gains go on the 2074."
        if total_foreign_value > 0
        else "nothing held abroad: your broker's IFU covers the declaration."
    )
    return [
        # Built directly: `flag` reads a threshold as ">=", which at zero would
        # report a duty for a book with nothing abroad.
        ReportingFlag(
            "formulaire_3916",
            total_foreign_value,
            FORM_3916_THRESHOLD,
            total_foreign_value > 0,
            msg,
        )
    ]
