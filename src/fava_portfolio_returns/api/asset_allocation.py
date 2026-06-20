import datetime
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from typing import Optional
from typing import Sequence

import yaml
from beancount.core.data import Directive
from beancount.core.data import Transaction
from beancount.core.inventory import Inventory
from beancount.core.number import ZERO
from fava.helpers import FavaAPIError

from fava_portfolio_returns.core.pricer import CurrencyConversionException
from fava_portfolio_returns.core.pricer import Pricer
from fava_portfolio_returns.core.utils import market_value_of_inv

# how close to 100% the target allocation must sum (rounding tolerance)
SUM_TOLERANCE = Decimal("0.1")


@dataclass(frozen=True)
class AssetAllocationConfig:
    """Configuration of a single portfolio's target asset allocation."""

    name: str
    # list of account regexes (OR-combined), matched like Beanquery's `~` operator
    accounts: list[str]
    # mapping of commodity -> target allocation in percent
    targets: dict[str, Decimal]


def parse_pct(value) -> Decimal:
    """Parse a target percentage, accepting both '25%' and bare numbers."""
    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()
    return Decimal(str(value))


def parse_portfolios(raw: Any) -> list[AssetAllocationConfig]:
    """Parse and minimally validate a list of portfolio definitions.

    Expects a list of portfolios, each with a `name`, a list of `accounts`
    (account regexes) and a list of `assets` (mappings with `commodity` and
    `target`).
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        raise FavaAPIError("'portfolios' must be a list of portfolios")

    portfolios = []
    for portfolio in raw:
        if not isinstance(portfolio, dict):
            raise FavaAPIError("each portfolio must be a mapping")
        name = portfolio.get("name", "<unnamed>")
        accounts = portfolio.get("accounts") or []
        if not isinstance(accounts, list) or not accounts:
            raise FavaAPIError(f"portfolio '{name}': missing 'accounts'")

        targets: dict[str, Decimal] = {}
        for asset in portfolio.get("assets") or []:
            if not isinstance(asset, dict) or "commodity" not in asset or "target" not in asset:
                raise FavaAPIError(f"portfolio '{name}': each asset needs a 'commodity' and a 'target'")
            targets[asset["commodity"]] = parse_pct(asset["target"])

        portfolios.append(AssetAllocationConfig(name=name, accounts=list(accounts), targets=targets))
    return portfolios


def load_asset_allocation_config(path: Path) -> list[AssetAllocationConfig]:
    """Load the target asset allocation from a YAML configuration file.

    The file format matches the `asset-allocation` CLI script, so the same
    file can be shared between the CLI and this plugin::

        portfolios:
          - name: My Portfolio
            accounts:                 # one or more account regexes
              - Assets:Broker:Investments:
            assets:                   # target allocation, should sum to 100%
              - commodity: ETF_FOO
                target: 60%
              - commodity: ETF_BAR
                target: 40%
    """
    try:
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except OSError as ex:
        raise FavaAPIError(f"Cannot read asset allocation configuration file {path}: {ex}") from ex

    if not isinstance(config, dict) or "portfolios" not in config:
        raise FavaAPIError(f"{path}: missing top-level 'portfolios' key.")
    return parse_portfolios(config["portfolios"])


def portfolio_holdings(
    entries: Sequence[Directive],
    pricer: Pricer,
    accounts: list[str],
    target_currency: str,
    end_date: datetime.date,
) -> tuple[dict[str, Decimal], list[str]]:
    """Return the current market value per commodity of a portfolio.

    `accounts` is a list of account regexes (OR-combined, matched unanchored
    like Beanquery's `~` operator). Holdings are valued in `target_currency` at
    the latest known prices as of `end_date`. The portfolio's cash holdings
    (positions already in `target_currency`) are ignored.

    Returns a pair ``(values, unpriced)`` where `values` maps commodity ->
    current market value, and `unpriced` lists held commodities that could not
    be valued (no known price).
    """
    patterns = [re.compile(account) for account in accounts]

    balances: dict[str, Inventory] = defaultdict(Inventory)
    for entry in entries:
        if not isinstance(entry, Transaction) or entry.date > end_date:
            continue
        for posting in entry.postings:
            if posting.units is None or posting.units.currency == target_currency:
                continue
            if any(pattern.search(posting.account) for pattern in patterns):
                balances[posting.units.currency].add_position(posting)

    values: dict[str, Decimal] = {}
    unpriced: list[str] = []
    for commodity, balance in balances.items():
        if balance.is_empty():
            continue  # no current holdings (e.g. fully liquidated)
        try:
            market_value = market_value_of_inv(pricer, target_currency, balance, end_date)
        except CurrencyConversionException:
            unpriced.append(commodity)
            continue
        if market_value != ZERO:
            values[commodity] = market_value
    return values, sorted(unpriced)


def report_portfolio(
    config: AssetAllocationConfig,
    values: dict[str, Decimal],
    target_currency: str,
    threshold: Decimal,
    names: dict[str, str],
) -> dict:
    """Build the asset allocation report for a single portfolio.

    Compares the current allocation (`values`) against the target allocation
    and computes, per configured commodity, the current and target weights and
    the deviation (in percentage points and in `target_currency`). A positive
    deviation means the position is overweight (suggesting to sell), a negative
    one underweight (suggesting to buy).
    """
    total_value = sum(values.values(), ZERO)

    assets = []
    diverged = False
    for commodity, target_pct in config.targets.items():
        cur_value = values.get(commodity, ZERO)
        cur_pct = (cur_value * 100 / total_value) if total_value else ZERO
        target_value = target_pct * total_value / 100
        dev_pct = cur_pct - target_pct  # positive => overweight
        dev_value = cur_value - target_value  # positive => overweight
        # Strict '>' is intentional: an asset exactly at the threshold is not flagged.
        over = abs(dev_pct) > threshold
        if over:
            diverged = True

        assets.append(
            {
                "commodity": commodity,
                "name": names.get(commodity, commodity),
                "targetPct": target_pct,
                "currentPct": cur_pct,
                "value": cur_value,
                "devPct": dev_pct,
                "devValue": dev_value,
                "over": over,
            }
        )

    # held commodities that are not part of the target allocation
    unconfigured = [
        {"commodity": commodity, "name": names.get(commodity, commodity), "value": values[commodity]}
        for commodity in sorted(set(values) - set(config.targets))
    ]

    target_sum = sum(config.targets.values(), ZERO)
    target_sum_ok = abs(target_sum - Decimal("100")) <= SUM_TOLERANCE

    return {
        "name": config.name,
        "accounts": config.accounts,
        "currency": target_currency,
        "totalValue": total_value,
        "threshold": threshold,
        "diverged": diverged,
        "assets": assets,
        "unconfigured": unconfigured,
        "targetSum": target_sum,
        "targetSumOk": target_sum_ok,
    }


def asset_allocation_report(
    entries: Sequence[Directive],
    pricer: Pricer,
    portfolios: list[AssetAllocationConfig],
    target_currency: str,
    end_date: datetime.date,
    threshold: Decimal,
    names: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Build the asset allocation report for all configured portfolios."""
    names = names or {}
    reports = []
    for config in portfolios:
        values, unpriced = portfolio_holdings(entries, pricer, config.accounts, target_currency, end_date)
        report = report_portfolio(config, values, target_currency, threshold, names)
        report["unpriced"] = unpriced
        reports.append(report)
    return reports
