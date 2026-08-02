import datetime
import re
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field
from decimal import ROUND_HALF_EVEN
from decimal import Decimal
from pathlib import Path
from typing import Any
from typing import Optional
from typing import Sequence

import yaml
from beancount.core.data import Commodity
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

# default commodity metadata key holding the (hierarchical) asset class
DEFAULT_ASSET_CLASS_KEY = "asset-class"

# target block name under a portfolio's 'allocation', per report kind
ALLOC_BLOCKS = {"commodity": "commodities", "class": "classes"}


@dataclass(frozen=True)
class AssetAllocationConfig:
    """Configuration of a single portfolio's target asset allocation.

    A portfolio's `allocation` may define per-commodity targets, per-asset-class
    targets, or both; each block (when present) should sum to 100%.
    """

    name: str
    # list of account regexes (OR-combined), matched like Beanquery's `~` operator
    accounts: list[str]
    # mapping of commodity -> target allocation in percent (insertion-ordered)
    commodity_targets: dict[str, Decimal] = field(default_factory=dict)
    # mapping of asset class -> target allocation in percent (insertion-ordered)
    class_targets: dict[str, Decimal] = field(default_factory=dict)
    # optional per-portfolio divergence threshold, overriding the file-level one
    divergence_threshold: Optional[Decimal] = None


@dataclass(frozen=True)
class AssetAllocationFile:
    """Parsed asset allocation configuration file."""

    portfolios: list[AssetAllocationConfig]
    # commodity metadata key holding the (hierarchical, ':'-separated) asset class
    asset_class_key: str
    # optional default divergence threshold (percentage points), None if unset
    divergence_threshold: Optional[Decimal]


def parse_pct(value) -> Decimal:
    """Parse a target percentage, accepting both '25%' and bare numbers."""
    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()
    return Decimal(str(value))


def quantize(amount: Decimal) -> Decimal:
    """Round a currency amount to cents (banker's rounding)."""
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def parse_threshold(value: Any, where: str) -> Decimal:
    """Parse and range-check a 'divergence-threshold' setting.

    `where` locates the setting in error messages (e.g. the file path or the
    portfolio it belongs to).
    """
    try:
        threshold = parse_pct(value)
    except (ArithmeticError, ValueError) as ex:
        raise FavaAPIError(f"{where}: invalid divergence-threshold '{value}'") from ex
    if threshold < ZERO or threshold > Decimal("100"):
        raise FavaAPIError(f"{where}: divergence-threshold {threshold}% is out of range [0, 100]")
    return threshold


def parse_targets(name: str, entries: Any, item_key: str, label: str) -> dict[str, Decimal]:
    """Parse and validate one target block (the 'commodities' or 'classes' list).

    `item_key` is the per-entry key naming the item ('commodity' or
    'asset-class'); `label` is a human word used in error messages. Returns an
    insertion-ordered mapping of item -> target percentage, raising a
    `FavaAPIError` on structural problems (missing keys, duplicates, invalid or
    out-of-range targets).
    """
    if not isinstance(entries, list):
        raise FavaAPIError(f"portfolio '{name}': '{label}' must be a list")

    targets: dict[str, Decimal] = {}
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise FavaAPIError(f"portfolio '{name}': {label} #{i} is not a mapping")
        if item_key not in entry:
            raise FavaAPIError(f"portfolio '{name}': {label} #{i} is missing '{item_key}'")
        item = entry[item_key]
        if item in targets:
            raise FavaAPIError(f"portfolio '{name}': duplicate {label} '{item}'")
        if "target" not in entry:
            raise FavaAPIError(f"portfolio '{name}': {label} '{item}' is missing 'target'")
        try:
            target = parse_pct(entry["target"])
        except (ArithmeticError, ValueError) as ex:
            raise FavaAPIError(
                f"portfolio '{name}': {label} '{item}' has an invalid target '{entry['target']}'"
            ) from ex
        if target < ZERO or target > Decimal("100"):
            raise FavaAPIError(f"portfolio '{name}': {label} '{item}' target {target}% is out of range [0, 100]")
        targets[item] = target
    return targets


def parse_allocation(name: str, portfolio: dict) -> Any:
    """Return a portfolio's `allocation` block, rejecting the pre-`allocation` layout.

    The target blocks used to sit directly in the portfolio; they now live under
    an explicit `allocation` key, shared with the asset-allocation CLI script.
    """
    allocation = portfolio.get("allocation")
    if allocation is None:
        legacy = [key for key in ALLOC_BLOCKS.values() if portfolio.get(key)]
        if legacy:
            raise FavaAPIError(
                f"portfolio '{name}': "
                + " and ".join(f"'{key}'" for key in legacy)
                + " must be nested under an 'allocation' key"
            )
        raise FavaAPIError(f"portfolio '{name}': no 'allocation' defined")
    if not isinstance(allocation, dict):
        raise FavaAPIError(f"portfolio '{name}': 'allocation' must be a mapping")
    return allocation


def parse_portfolios(raw: Any) -> list[AssetAllocationConfig]:
    """Parse and minimally validate a list of portfolio definitions.

    Each portfolio has a `name`, a list of `accounts` (account regexes) and an
    `allocation` block holding `commodities` and/or `classes` (at least one is
    required), each a list of mappings (`commodity`/`asset-class` plus
    `target`). A portfolio may also carry its own `divergence-threshold`.
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        raise FavaAPIError("'portfolios' must be a list of portfolios")

    portfolios = []
    seen_names: set[str] = set()
    for portfolio in raw:
        if not isinstance(portfolio, dict):
            raise FavaAPIError("each portfolio must be a mapping")
        name = portfolio.get("name", "<unnamed>")
        # reject duplicate names: they make the report and its React keys ambiguous
        if name in seen_names:
            raise FavaAPIError(f"duplicate portfolio name '{name}'")
        seen_names.add(name)
        accounts = portfolio.get("accounts") or []
        if not isinstance(accounts, list) or not accounts:
            raise FavaAPIError(f"portfolio '{name}': missing 'accounts'")

        threshold = portfolio.get("divergence-threshold")
        if threshold is not None:
            threshold = parse_threshold(threshold, f"portfolio '{name}'")

        allocation = parse_allocation(name, portfolio)
        commodities = allocation.get(ALLOC_BLOCKS["commodity"])
        classes = allocation.get(ALLOC_BLOCKS["class"])
        if not commodities and not classes:
            raise FavaAPIError(f"portfolio '{name}': allocation defines no 'commodities' or 'classes'")

        commodity_targets = parse_targets(name, commodities, "commodity", "commodity") if commodities else {}
        class_targets = parse_targets(name, classes, "asset-class", "class") if classes else {}

        portfolios.append(
            AssetAllocationConfig(
                name=name,
                accounts=list(accounts),
                commodity_targets=commodity_targets,
                class_targets=class_targets,
                divergence_threshold=threshold,
            )
        )
    return portfolios


def load_asset_allocation_config(path: Path) -> AssetAllocationFile:
    """Load the target asset allocation from a YAML configuration file.

    The file format matches the `asset-allocation` CLI script, so the same file
    can be shared between the CLI and this plugin::

        asset-class-key: asset-class  # optional; commodity metadata key holding
                                      # the (hierarchical) asset class
        divergence-threshold: 5%      # optional; default divergence threshold
        portfolios:
          - name: My Portfolio
            accounts:                 # one or more account regexes
              - Assets:Broker:Investments:
            divergence-threshold: 3%  # optional; overrides the file-level one
            allocation:               # the target asset allocation, by...
              commodities:            # ... commodity, should sum to 100%
                - commodity: ETF_FOO
                  target: 60%
                - commodity: ETF_BAR
                  target: 40%
              classes:                # ... asset class, should sum to 100%
                - asset-class: stocks
                  target: 70%
                - asset-class: bonds
                  target: 30%
    """
    try:
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except OSError as ex:
        raise FavaAPIError(f"Cannot read asset allocation configuration file {path}: {ex}") from ex

    if not isinstance(config, dict) or "portfolios" not in config:
        raise FavaAPIError(f"{path}: missing top-level 'portfolios' key.")

    asset_class_key = config.get("asset-class-key", DEFAULT_ASSET_CLASS_KEY)

    divergence_threshold = None
    if config.get("divergence-threshold") is not None:
        divergence_threshold = parse_threshold(config["divergence-threshold"], str(path))

    return AssetAllocationFile(
        portfolios=parse_portfolios(config["portfolios"]),
        asset_class_key=asset_class_key,
        divergence_threshold=divergence_threshold,
    )


def commodity_asset_classes(entries: Sequence[Directive], key: str) -> dict[str, str]:
    """Map commodity -> its asset-class metadata value.

    Built from the ledger's Commodity directives, reading metadata key `key`.
    Commodities without that metadata are omitted.
    """
    classes: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, Commodity):
            value = entry.meta.get(key)
            if value is not None:
                classes[entry.currency] = str(value)
    return classes


def match_class(asset_class: str, targets: list[str]) -> Optional[str]:
    """Return the most-specific configured target matching `asset_class`.

    A target ``T`` matches if ``asset_class == T`` or ``asset_class`` starts
    with ``T + ':'`` (asset classes are hierarchical, ':'-separated). Among all
    matching targets the longest (most specific) one wins. Return None if none
    matches.
    """
    best = None
    for target in targets:
        if asset_class == target or asset_class.startswith(target + ":"):
            if best is None or len(target) > len(best):
                best = target
    return best


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
    patterns = []
    for account in accounts:
        try:
            patterns.append(re.compile(account))
        except re.error as ex:
            raise FavaAPIError(f"invalid account pattern '{account}': {ex}") from ex

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


def _apply_residual(
    assignments: list[tuple[str, Decimal, Decimal]], target_total: Decimal
) -> list[tuple[str, Decimal, Decimal]]:
    """Nudge the first assignment so the (cent-rounded) amounts sum exactly.

    `assignments` is a list of ``(commodity, value, amount)`` ordered with the
    largest trade first; the rounding residual against `target_total` is added
    to that first trade.
    """
    if not assignments:
        return assignments
    residual = quantize(target_total) - sum((a for _, _, a in assignments), ZERO)
    commodity, value, amount = assignments[0]
    assignments[0] = (commodity, value, amount + residual)
    return assignments


def proportional_split(
    dev_value: Decimal, members: dict[str, Decimal], cur_value: Decimal
) -> list[tuple[str, Decimal, Decimal]]:
    """Split a class gap across all members proportional to current value.

    Return a list of ``(commodity, value, amount)`` (largest value first) whose
    amounts sum exactly to `dev_value` (rounded to cents). Each amount carries
    the sign of `dev_value` (positive => sell, negative => buy).
    """
    members_sorted = sorted(members.items(), key=lambda kv: kv[1], reverse=True)
    assignments = [(commodity, value, quantize(dev_value * value / cur_value)) for commodity, value in members_sorted]
    return _apply_residual(assignments, dev_value)


def minimal_split(
    dev_value: Decimal,
    members: dict[str, Decimal],
    commodity_targets: dict[str, Decimal],
    total_value: Decimal,
    threshold: Decimal,
) -> tuple[list[tuple[str, Decimal, Decimal]], Decimal]:
    """Close a class gap using the fewest commodities.

    Greedily allocate ``abs(dev_value)`` to the members with the most tradeable
    *capacity* first; each member's capacity is bounded so the trade does not
    push its own allocation beyond ±`threshold` of its commodity target (when
    `commodity_targets` lists it; otherwise unbounded).

    Return ``(assignments, shortfall)`` where `assignments` is a list of
    ``(commodity, value, amount)`` for the traded commodities only (amounts
    signed like `dev_value`), and `shortfall` is the amount left uncovered when
    capacities cannot reach the full gap.
    """
    selling = dev_value > 0
    gap = abs(dev_value)

    capacities = []
    for commodity, value in members.items():
        target_pct = commodity_targets.get(commodity)
        if selling:
            if target_pct is None:
                floor = ZERO
            else:
                floor = max(ZERO, (target_pct - threshold) / 100 * total_value)
            cap = min(max(value - floor, ZERO), value)
        else:
            if target_pct is None:
                cap = Decimal("Infinity")
            else:
                ceil = (target_pct + threshold) / 100 * total_value
                cap = max(ceil - value, ZERO)
        if cap > 0:
            capacities.append((commodity, value, cap))

    # most capacity first => fewest commodities; deterministic tie-breaks
    capacities.sort(key=lambda cvc: (cvc[2], cvc[1], cvc[0]), reverse=True)

    assignments = []
    remaining = gap
    sign = Decimal(1) if selling else Decimal(-1)
    for commodity, value, cap in capacities:
        if remaining <= 0:
            break
        take = min(remaining, cap)
        assignments.append((commodity, value, sign * quantize(take)))
        remaining -= take

    shortfall = max(remaining, ZERO)
    if shortfall <= 0:
        # exact reconciliation: absorb cent residual into the largest trade
        assignments = _apply_residual(assignments, dev_value)
    return assignments, quantize(shortfall)


def _deviation(cur_value: Decimal, target_pct: Decimal, total_value: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(cur_pct, dev_pct, dev_value)`` for a holding vs. its target.

    A positive deviation means overweight (suggesting to sell), negative
    underweight (suggesting to buy).
    """
    cur_pct = (cur_value * 100 / total_value) if total_value else ZERO
    target_value = target_pct * total_value / 100
    return cur_pct, cur_pct - target_pct, cur_value - target_value


def _target_sum(targets: dict[str, Decimal]) -> tuple[Decimal, bool]:
    total = sum(targets.values(), ZERO)
    return total, abs(total - Decimal("100")) <= SUM_TOLERANCE


def report_portfolio_commodities(
    config: AssetAllocationConfig,
    values: dict[str, Decimal],
    threshold: Decimal,
    names: dict[str, str],
) -> dict:
    """Build the by-commodity sub-report for a single portfolio.

    Compares the current allocation (`values`) against the target allocation and
    computes, per configured commodity, the current and target weights and the
    deviation (in percentage points and in the target currency).
    """
    total_value = sum(values.values(), ZERO)

    assets = []
    diverged = False
    for commodity, target_pct in config.commodity_targets.items():
        cur_value = values.get(commodity, ZERO)
        cur_pct, dev_pct, dev_value = _deviation(cur_value, target_pct, total_value)
        # Strict '>' is intentional: an asset exactly at the threshold is not flagged.
        over = abs(dev_pct) > threshold
        diverged = diverged or over

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
        for commodity in sorted(set(values) - set(config.commodity_targets))
    ]

    target_sum, target_sum_ok = _target_sum(config.commodity_targets)
    return {
        "diverged": diverged,
        "assets": assets,
        "unconfigured": unconfigured,
        "targetSum": target_sum,
        "targetSumOk": target_sum_ok,
    }


def report_portfolio_classes(
    config: AssetAllocationConfig,
    values: dict[str, Decimal],
    asset_classes: dict[str, str],
    threshold: Decimal,
    names: dict[str, str],
    minimize: bool,
) -> dict:
    """Build the by-asset-class sub-report for a single portfolio.

    Each held commodity is bucketed into its most-specific configured class.
    Under each class, concrete per-commodity buy/sell suggestions are computed;
    in aggregate they close the class's gap (proportional to current value by
    default; by fewest commodities with `minimize`).
    """
    targets = list(config.class_targets)
    total_value = sum(values.values(), ZERO)

    # bucket held commodities into the configured classes
    members: dict[str, dict[str, Decimal]] = {t: {} for t in targets}
    unconfigured = []  # holdings without metadata or with an untargeted class
    for commodity, value in values.items():
        asset_class = asset_classes.get(commodity)
        if asset_class is None:
            unconfigured.append((commodity, value, "has no asset-class metadata"))
            continue
        bucket = match_class(asset_class, targets)
        if bucket is None:
            unconfigured.append((commodity, value, f"asset class '{asset_class}' is not targeted"))
            continue
        members[bucket][commodity] = value

    classes = []
    diverged = False
    for asset_class in targets:
        target_pct = config.class_targets[asset_class]
        holdings = members[asset_class]
        cur_value = sum(holdings.values(), ZERO)
        cur_pct, dev_pct, dev_value = _deviation(cur_value, target_pct, total_value)
        over = abs(dev_pct) > threshold
        diverged = diverged or over

        # per-commodity rebalance breakdown for this class
        member_trades = []
        shortfall = ZERO
        if ZERO not in (dev_value, cur_value):
            if minimize:
                shares, shortfall = minimal_split(dev_value, holdings, config.commodity_targets, total_value, threshold)
            else:
                shares = proportional_split(dev_value, holdings, cur_value)
            member_trades = [
                {
                    "commodity": commodity,
                    "name": names.get(commodity, commodity),
                    "value": value,
                    "tradeValue": amount,  # positive => sell, negative => buy
                }
                for commodity, value, amount in shares
            ]

        classes.append(
            {
                "assetClass": asset_class,
                "targetPct": target_pct,
                "currentPct": cur_pct,
                "value": cur_value,
                "devPct": dev_pct,
                "devValue": dev_value,
                "over": over,
                "members": member_trades,
                "noHoldings": cur_value == ZERO,
                "shortfall": shortfall,
            }
        )

    target_sum, target_sum_ok = _target_sum(config.class_targets)
    return {
        "diverged": diverged,
        "minimize": minimize,
        "classes": classes,
        "unconfigured": [
            {"commodity": commodity, "name": names.get(commodity, commodity), "value": value, "reason": reason}
            for commodity, value, reason in sorted(unconfigured)
        ],
        "targetSum": target_sum,
        "targetSumOk": target_sum_ok,
    }


def resolve_threshold(config: AssetAllocationConfig, override: Optional[Decimal], default: Decimal) -> Decimal:
    """Pick the divergence threshold to apply to one portfolio.

    Precedence, most specific first: `override` (the directive option, which
    applies to every portfolio), the portfolio's own `divergence-threshold`, and
    `default` (the file-level setting, or the code default).
    """
    for candidate in (override, config.divergence_threshold):
        if candidate is not None:
            return candidate
    return default


def asset_allocation_report(
    entries: Sequence[Directive],
    pricer: Pricer,
    portfolios: list[AssetAllocationConfig],
    target_currency: str,
    end_date: datetime.date,
    threshold: Decimal,
    asset_classes: Optional[dict[str, str]] = None,
    names: Optional[dict[str, str]] = None,
    minimize: bool = False,
    threshold_override: Optional[Decimal] = None,
) -> list[dict]:
    """Build the asset allocation report for all configured portfolios.

    Each portfolio emits a `commodityReport` and/or a `classReport` sub-report,
    depending on which target blocks it defines. `threshold` applies to the
    portfolios that do not set their own; `threshold_override` (when given)
    applies to all of them (see `resolve_threshold`).
    """
    asset_classes = asset_classes or {}
    names = names or {}
    reports = []
    for config in portfolios:
        values, unpriced = portfolio_holdings(entries, pricer, config.accounts, target_currency, end_date)
        portfolio_threshold = resolve_threshold(config, threshold_override, threshold)
        report: dict[str, Any] = {
            "name": config.name,
            "accounts": config.accounts,
            "currency": target_currency,
            "totalValue": sum(values.values(), ZERO),
            "threshold": portfolio_threshold,
            "unpriced": unpriced,
            "commodityReport": None,
            "classReport": None,
        }
        if config.commodity_targets:
            report["commodityReport"] = report_portfolio_commodities(config, values, portfolio_threshold, names)
        if config.class_targets:
            report["classReport"] = report_portfolio_classes(
                config, values, asset_classes, portfolio_threshold, names, minimize
            )
        reports.append(report)
    return reports
