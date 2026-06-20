from decimal import Decimal

import pytest
from beancount import loader
from beancount.core import prices
from beancount.core.data import Commodity
from fava.helpers import FavaAPIError

from fava_portfolio_returns.api.asset_allocation import AssetAllocationConfig
from fava_portfolio_returns.api.asset_allocation import asset_allocation_report
from fava_portfolio_returns.api.asset_allocation import load_asset_allocation_config
from fava_portfolio_returns.api.asset_allocation import parse_pct
from fava_portfolio_returns.api.asset_allocation import parse_portfolios
from fava_portfolio_returns.core.pricer import Pricer

LEDGER = """
option "operating_currency" "USD"

2020-01-01 open Assets:Broker:AAA
2020-01-01 open Assets:Broker:BBB
2020-01-01 open Assets:Cash

2020-01-01 commodity AAA
  name: "Fund AAA"

2020-01-01 commodity BBB

2020-01-01 * "buy AAA"
  Assets:Broker:AAA  10 AAA {10 USD}
  Assets:Cash

2020-01-01 * "buy BBB"
  Assets:Broker:BBB  10 BBB {10 USD}
  Assets:Cash

2021-01-01 price AAA  12 USD
2021-01-01 price BBB   8 USD
"""


def build_report(targets, threshold=Decimal("5"), accounts=None):
    entries, errors, _options = loader.load_string(LEDGER)
    assert not errors, errors
    pricer = Pricer(prices.build_price_map(entries))
    config = AssetAllocationConfig(
        name="test",
        accounts=accounts or ["Assets:Broker:"],
        targets=targets,
    )
    reports = asset_allocation_report(
        entries,
        pricer,
        [config],
        "USD",
        entries[-1].date,
        threshold,
        names={e.currency: e.meta["name"] for e in entries if isinstance(e, Commodity) and e.meta.get("name")},
    )
    return reports[0]


def test_parse_pct():
    assert parse_pct("25%") == Decimal("25")
    assert parse_pct("25") == Decimal("25")
    assert parse_pct(25) == Decimal("25")


def test_parse_portfolios():
    raw = [
        {
            "name": "p1",
            "accounts": ["Assets:Broker:"],
            "assets": [{"commodity": "AAA", "target": "60%"}, {"commodity": "BBB", "target": "40%"}],
        }
    ]
    [portfolio] = parse_portfolios(raw)
    assert portfolio.name == "p1"
    assert portfolio.accounts == ["Assets:Broker:"]
    assert portfolio.targets == {"AAA": Decimal("60"), "BBB": Decimal("40")}


def test_load_config(tmp_path):
    config_file = tmp_path / "asset-allocation.yaml"
    config_file.write_text(
        """
portfolios:
  - name: p1
    accounts:
      - "Assets:Broker:"
    assets:
      - commodity: AAA
        target: 60%
      - commodity: BBB
        target: 40%
"""
    )
    [portfolio] = load_asset_allocation_config(config_file)
    assert portfolio.name == "p1"
    assert portfolio.accounts == ["Assets:Broker:"]
    assert portfolio.targets == {"AAA": Decimal("60"), "BBB": Decimal("40")}


def test_load_config_missing_portfolios_key(tmp_path):
    config_file = tmp_path / "bad.yaml"
    config_file.write_text("foo: bar\n")
    with pytest.raises(FavaAPIError):
        load_asset_allocation_config(config_file)


def test_allocation_and_deviation():
    report = build_report({"AAA": Decimal("50"), "BBB": Decimal("50")})

    assert report["totalValue"] == Decimal("200")  # 10*12 + 10*8
    assert report["diverged"] is True  # +/-10pp > 5pp threshold
    assert report["targetSumOk"] is True

    by_commodity = {a["commodity"]: a for a in report["assets"]}
    aaa = by_commodity["AAA"]
    assert aaa["name"] == "Fund AAA"
    assert aaa["value"] == Decimal("120")
    assert aaa["currentPct"] == Decimal("60")
    assert aaa["devPct"] == Decimal("10")
    assert aaa["devValue"] == Decimal("20")  # overweight => sell 20
    assert aaa["over"] is True

    bbb = by_commodity["BBB"]
    assert bbb["value"] == Decimal("80")
    assert bbb["currentPct"] == Decimal("40")
    assert bbb["devValue"] == Decimal("-20")  # underweight => buy 20


def test_unconfigured_commodity():
    # only AAA is configured; BBB is held but not in the target allocation
    report = build_report({"AAA": Decimal("100")})
    assert [u["commodity"] for u in report["unconfigured"]] == ["BBB"]
    assert report["unconfigured"][0]["value"] == Decimal("80")


def test_within_threshold_not_diverged():
    # target close to actual (60/40), within 5pp threshold
    report = build_report({"AAA": Decimal("60"), "BBB": Decimal("40")})
    assert report["diverged"] is False


def test_target_sum_validation():
    report = build_report({"AAA": Decimal("50"), "BBB": Decimal("30")})
    assert report["targetSum"] == Decimal("80")
    assert report["targetSumOk"] is False
