from decimal import Decimal

import pytest
from beancount import loader
from beancount.core import prices
from beancount.core.data import Commodity
from fava.helpers import FavaAPIError

from fava_portfolio_returns.api.asset_allocation import AssetAllocationConfig
from fava_portfolio_returns.api.asset_allocation import asset_allocation_report
from fava_portfolio_returns.api.asset_allocation import commodity_asset_classes
from fava_portfolio_returns.api.asset_allocation import load_asset_allocation_config
from fava_portfolio_returns.api.asset_allocation import match_class
from fava_portfolio_returns.api.asset_allocation import minimal_split
from fava_portfolio_returns.api.asset_allocation import parse_pct
from fava_portfolio_returns.api.asset_allocation import parse_portfolios
from fava_portfolio_returns.api.asset_allocation import proportional_split
from fava_portfolio_returns.core.pricer import Pricer

LEDGER = """
option "operating_currency" "USD"

2020-01-01 open Assets:Broker:AAA
2020-01-01 open Assets:Broker:BBB
2020-01-01 open Assets:Cash

2020-01-01 commodity AAA
  name: "Fund AAA"
  asset-class: "stocks"

2020-01-01 commodity BBB
  asset-class: "bonds"

2020-01-01 * "buy AAA"
  Assets:Broker:AAA  10 AAA {10 USD}
  Assets:Cash

2020-01-01 * "buy BBB"
  Assets:Broker:BBB  10 BBB {10 USD}
  Assets:Cash

2021-01-01 price AAA  12 USD
2021-01-01 price BBB   8 USD
"""


def _load():
    entries, errors, _options = loader.load_string(LEDGER)
    assert not errors, errors
    pricer = Pricer(prices.build_price_map(entries))
    names = {e.currency: e.meta["name"] for e in entries if isinstance(e, Commodity) and e.meta.get("name")}
    return entries, pricer, names


def build_report(commodity_targets=None, class_targets=None, threshold=Decimal("5"), accounts=None, minimize=False):
    entries, pricer, names = _load()
    config = AssetAllocationConfig(
        name="test",
        accounts=accounts or ["Assets:Broker:"],
        commodity_targets=commodity_targets or {},
        class_targets=class_targets or {},
    )
    reports = asset_allocation_report(
        entries,
        pricer,
        [config],
        "USD",
        entries[-1].date,
        threshold,
        asset_classes=commodity_asset_classes(entries, "asset-class"),
        names=names,
        minimize=minimize,
    )
    return reports[0]


def test_parse_pct():
    assert parse_pct("25%") == Decimal("25")
    assert parse_pct("25") == Decimal("25")
    assert parse_pct(25) == Decimal("25")


def test_parse_portfolios_commodities_and_classes():
    raw = [
        {
            "name": "p1",
            "accounts": ["Assets:Broker:"],
            "commodities": [{"commodity": "AAA", "target": "60%"}, {"commodity": "BBB", "target": "40%"}],
            "classes": [{"asset-class": "stocks", "target": "70%"}, {"asset-class": "bonds", "target": "30%"}],
        }
    ]
    [portfolio] = parse_portfolios(raw)
    assert portfolio.name == "p1"
    assert portfolio.accounts == ["Assets:Broker:"]
    assert portfolio.commodity_targets == {"AAA": Decimal("60"), "BBB": Decimal("40")}
    assert portfolio.class_targets == {"stocks": Decimal("70"), "bonds": Decimal("30")}


def test_parse_portfolios_requires_a_target_block():
    with pytest.raises(FavaAPIError):
        parse_portfolios([{"name": "p1", "accounts": ["Assets:Broker:"]}])


def test_parse_portfolios_rejects_duplicate_and_out_of_range():
    with pytest.raises(FavaAPIError):
        parse_portfolios(
            [
                {
                    "name": "p1",
                    "accounts": ["Assets:Broker:"],
                    "commodities": [{"commodity": "AAA", "target": "60%"}, {"commodity": "AAA", "target": "40%"}],
                }
            ]
        )
    with pytest.raises(FavaAPIError):
        parse_portfolios(
            [
                {
                    "name": "p1",
                    "accounts": ["Assets:Broker:"],
                    "classes": [{"asset-class": "stocks", "target": "150%"}],
                }
            ]
        )


def test_parse_portfolios_rejects_duplicate_names():
    raw = [
        {"name": "p1", "accounts": ["Assets:Broker:"], "commodities": [{"commodity": "AAA", "target": "100%"}]},
        {"name": "p1", "accounts": ["Assets:Broker:"], "commodities": [{"commodity": "BBB", "target": "100%"}]},
    ]
    with pytest.raises(FavaAPIError):
        parse_portfolios(raw)


def test_invalid_account_pattern_raises_fava_error():
    with pytest.raises(FavaAPIError):
        build_report(commodity_targets={"AAA": Decimal("100")}, accounts=["Assets:[Broker"])


def test_load_config(tmp_path):
    config_file = tmp_path / "asset-allocation.yaml"
    config_file.write_text(
        """
asset-class-key: my-class
divergence-threshold: 7%
portfolios:
  - name: p1
    accounts:
      - "Assets:Broker:"
    commodities:
      - commodity: AAA
        target: 60%
      - commodity: BBB
        target: 40%
    classes:
      - asset-class: stocks
        target: 60%
      - asset-class: bonds
        target: 40%
"""
    )
    config = load_asset_allocation_config(config_file)
    assert config.asset_class_key == "my-class"
    assert config.divergence_threshold == Decimal("7")
    [portfolio] = config.portfolios
    assert portfolio.commodity_targets == {"AAA": Decimal("60"), "BBB": Decimal("40")}
    assert portfolio.class_targets == {"stocks": Decimal("60"), "bonds": Decimal("40")}


def test_load_config_defaults(tmp_path):
    config_file = tmp_path / "asset-allocation.yaml"
    config_file.write_text(
        """
portfolios:
  - name: p1
    accounts:
      - "Assets:Broker:"
    commodities:
      - commodity: AAA
        target: 100%
"""
    )
    config = load_asset_allocation_config(config_file)
    assert config.asset_class_key == "asset-class"
    assert config.divergence_threshold is None


def test_load_config_missing_portfolios_key(tmp_path):
    config_file = tmp_path / "bad.yaml"
    config_file.write_text("foo: bar\n")
    with pytest.raises(FavaAPIError):
        load_asset_allocation_config(config_file)


def test_commodity_report_allocation_and_deviation():
    report = build_report(commodity_targets={"AAA": Decimal("50"), "BBB": Decimal("50")})

    assert report["totalValue"] == Decimal("200")  # 10*12 + 10*8
    assert report["classReport"] is None
    commodity = report["commodityReport"]
    assert commodity["diverged"] is True  # +/-10pp > 5pp threshold
    assert commodity["targetSumOk"] is True

    by_commodity = {a["commodity"]: a for a in commodity["assets"]}
    aaa = by_commodity["AAA"]
    assert aaa["name"] == "Fund AAA"
    assert aaa["value"] == Decimal("120")
    assert aaa["currentPct"] == Decimal("60")
    assert aaa["devPct"] == Decimal("10")
    assert aaa["devValue"] == Decimal("20")  # overweight => sell 20
    assert aaa["over"] is True

    bbb = by_commodity["BBB"]
    assert bbb["value"] == Decimal("80")
    assert bbb["devValue"] == Decimal("-20")  # underweight => buy 20


def test_commodity_report_unconfigured():
    # only AAA is configured; BBB is held but not in the target allocation
    report = build_report(commodity_targets={"AAA": Decimal("100")})
    unconfigured = report["commodityReport"]["unconfigured"]
    assert [u["commodity"] for u in unconfigured] == ["BBB"]
    assert unconfigured[0]["value"] == Decimal("80")


def test_commodity_report_within_threshold():
    report = build_report(commodity_targets={"AAA": Decimal("60"), "BBB": Decimal("40")})
    assert report["commodityReport"]["diverged"] is False


def test_commodity_report_target_sum_validation():
    report = build_report(commodity_targets={"AAA": Decimal("50"), "BBB": Decimal("30")})
    commodity = report["commodityReport"]
    assert commodity["targetSum"] == Decimal("80")
    assert commodity["targetSumOk"] is False


def test_match_class_hierarchical():
    targets = ["stocks", "stocks:health", "bonds"]
    assert match_class("stocks", targets) == "stocks"
    assert match_class("stocks:health", targets) == "stocks:health"  # most specific wins
    assert match_class("stocks:world", targets) == "stocks"  # falls back to parent
    assert match_class("cash", targets) is None


def test_class_report_bucketing_and_deviation():
    # AAA (stocks) = 120, BBB (bonds) = 80, total 200
    report = build_report(class_targets={"stocks": Decimal("50"), "bonds": Decimal("50")})
    assert report["commodityReport"] is None
    class_report = report["classReport"]
    assert class_report["diverged"] is True

    by_class = {c["assetClass"]: c for c in class_report["classes"]}
    stocks = by_class["stocks"]
    assert stocks["value"] == Decimal("120")
    assert stocks["currentPct"] == Decimal("60")
    assert stocks["devValue"] == Decimal("20")  # overweight => sell 20
    # single member: whole class trade lands on AAA
    assert [(m["commodity"], m["tradeValue"]) for m in stocks["members"]] == [("AAA", Decimal("20"))]

    bonds = by_class["bonds"]
    assert bonds["devValue"] == Decimal("-20")
    assert [(m["commodity"], m["tradeValue"]) for m in bonds["members"]] == [("BBB", Decimal("-20"))]


def test_class_report_unconfigured():
    # bonds class not configured, so BBB has no targeted class
    report = build_report(class_targets={"stocks": Decimal("100")})
    unconfigured = report["classReport"]["unconfigured"]
    assert [u["commodity"] for u in unconfigured] == ["BBB"]
    assert "not targeted" in unconfigured[0]["reason"]


def test_proportional_split_sums_exactly():
    members = {"AAA": Decimal("70"), "BBB": Decimal("30")}
    shares = proportional_split(Decimal("10.00"), members, Decimal("100"))
    assert sum(amount for _, _, amount in shares) == Decimal("10.00")
    # largest holding first, split proportionally
    assert shares[0][0] == "AAA"


def test_minimal_split_respects_capacity_and_reports_shortfall():
    members = {"AAA": Decimal("70"), "BBB": Decimal("30")}
    commodity_targets = {"AAA": Decimal("65"), "BBB": Decimal("35")}
    # sell 10 from a 100 total; AAA capacity down to (65-5)% = 60 => 10 capacity
    assignments, shortfall = minimal_split(
        Decimal("10"), members, commodity_targets, Decimal("100"), Decimal("5")
    )
    assert shortfall == Decimal("0.00")
    assert sum(a for _, _, a in assignments) == Decimal("10.00")

    # tiny capacities => cannot cover the whole gap
    tight = {"AAA": Decimal("66"), "BBB": Decimal("34")}
    assignments, shortfall = minimal_split(Decimal("20"), members, tight, Decimal("100"), Decimal("5"))
    assert shortfall > Decimal("0")


def test_both_reports_present():
    report = build_report(
        commodity_targets={"AAA": Decimal("60"), "BBB": Decimal("40")},
        class_targets={"stocks": Decimal("60"), "bonds": Decimal("40")},
    )
    assert report["commodityReport"] is not None
    assert report["classReport"] is not None
