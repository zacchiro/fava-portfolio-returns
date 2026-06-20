import { Alert, Stack, useTheme } from "@mui/material";
import { DataGrid, GridColDef } from "@mui/x-data-grid";
import { createRoute } from "@tanstack/react-router";
import { EChartsOption } from "echarts";
import { useTranslation } from "react-i18next";
import { AssetAllocationAsset, AssetAllocationPortfolio, useAssetAllocation } from "../api/asset_allocation";
import { Dashboard, DashboardRow, Panel } from "../components/Dashboard";
import { EChart } from "../components/EChart";
import { useCurrencyFormatter, usePercentFormatter } from "../components/format";
import { useToolbarContext } from "../components/Header/ToolbarProvider";
import { Loading } from "../components/Loading";
import { RootRoute } from "./__root";

export const AssetAllocationRoute = createRoute({
  getParentRoute: () => RootRoute,
  path: "asset_allocation",
  staticData: {
    showInvestmentsSelection: false,
  },
  component: AssetAllocation,
});

const EXAMPLE_CONFIG = `2010-01-01 custom "fava-extension" "fava_portfolio_returns" "{
  'beangrow_config': 'beangrow.pbtxt',
  'asset_allocation_threshold': 5,
  'asset_allocation': [
    {
      'name': 'My Portfolio',
      'accounts': ['Assets:Broker:Investments:'],
      'assets': [
        {'commodity': 'ETF_FOO', 'target': '60%'},
        {'commodity': 'ETF_BAR', 'target': '40%'},
      ],
    },
  ],
}"`;

function AssetAllocation() {
  const { t } = useTranslation();
  const { targetCurrency } = useToolbarContext();
  const { isPending, error, data } = useAssetAllocation({ targetCurrency });

  if (isPending) {
    return <Loading />;
  }
  if (error) {
    return <Alert severity="error">{error.message}</Alert>;
  }

  if (data.portfolios.length === 0) {
    return (
      <Dashboard>
        <DashboardRow>
          <Panel
            title={t("Asset Allocation")}
            help={t(
              "Compares the current asset allocation of your portfolios against a target allocation. Define one or more portfolios in the 'asset_allocation' option of the extension configuration:",
            )}
          >
            <pre>{EXAMPLE_CONFIG}</pre>
          </Panel>
        </DashboardRow>
      </Dashboard>
    );
  }

  return (
    <Dashboard>
      {data.portfolios.map((portfolio) => (
        <DashboardRow key={portfolio.name}>
          <PortfolioReport portfolio={portfolio} />
        </DashboardRow>
      ))}
    </Dashboard>
  );
}

function PortfolioReport({ portfolio }: { portfolio: AssetAllocationPortfolio }) {
  const { t } = useTranslation();
  const currencyFormatter = useCurrencyFormatter(portfolio.currency);

  const help = t("Accounts: {{accounts}} — Total value: {{total}}", {
    accounts: portfolio.accounts.join(", "),
    total: currencyFormatter(portfolio.totalValue),
  });

  return (
    <Panel title={portfolio.name} help={help} sx={{ flex: 1 }}>
      <Stack sx={{ gap: 1 }}>
        {portfolio.diverged ? (
          <Alert severity="warning">
            {t("Some assets diverge by more than ±{{threshold}}% from the target allocation.", {
              threshold: portfolio.threshold,
            })}
          </Alert>
        ) : (
          <Alert severity="success">
            {t("All assets are within ±{{threshold}}% of the target allocation.", {
              threshold: portfolio.threshold,
            })}
          </Alert>
        )}
        {!portfolio.targetSumOk && (
          <Alert severity="error">
            {t("The target allocation sums to {{sum}}%, expected 100%.", { sum: portfolio.targetSum })}
          </Alert>
        )}
        {portfolio.unconfigured.map((holding) => (
          <Alert severity="warning" key={holding.commodity}>
            {t("Held commodity {{commodity}} ({{value}}) is not part of the target allocation.", {
              commodity: holding.commodity,
              value: currencyFormatter(holding.value),
            })}
          </Alert>
        ))}
        {portfolio.unpriced.map((commodity) => (
          <Alert severity="warning" key={commodity}>
            {t("Held commodity {{commodity}} has no known price and is excluded from the valuation.", {
              commodity: commodity,
            })}
          </Alert>
        ))}
        <AllocationChart portfolio={portfolio} />
        <AllocationTable portfolio={portfolio} />
      </Stack>
    </Panel>
  );
}

function AllocationChart({ portfolio }: { portfolio: AssetAllocationPortfolio }) {
  const { t } = useTranslation();
  const percentFormatter = usePercentFormatter();
  // reverse so the first asset appears at the top of the (inverted) category axis
  const assets = [...portfolio.assets].reverse();

  const option: EChartsOption = {
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      valueFormatter: (value) => (typeof value === "number" ? percentFormatter(value / 100) : String(value)),
    },
    legend: { bottom: 0 },
    grid: { left: 120, right: 20, top: 10, bottom: 30 },
    xAxis: {
      type: "value",
      axisLabel: { formatter: (value: number) => percentFormatter(value / 100) },
    },
    yAxis: {
      type: "category",
      data: assets.map((asset) => asset.commodity),
    },
    series: [
      { type: "bar", name: t("Target"), data: assets.map((asset) => asset.targetPct) },
      { type: "bar", name: t("Current"), data: assets.map((asset) => asset.currentPct) },
    ],
  };

  const height = `${Math.max(120, portfolio.assets.length * 34 + 60)}px`;
  return <EChart height={height} option={option} />;
}

function AllocationTable({ portfolio }: { portfolio: AssetAllocationPortfolio }) {
  const { t } = useTranslation();
  const theme = useTheme();
  const currencyFormatter = useCurrencyFormatter(portfolio.currency);
  const percentFormatter = usePercentFormatter({ fixed: true });

  const rebalance = (devValue: number) => {
    if (Math.abs(devValue) < 0.01) {
      return "—";
    }
    return devValue > 0
      ? t("Sell {{amount}}", { amount: currencyFormatter(Math.abs(devValue)) })
      : t("Buy {{amount}}", { amount: currencyFormatter(Math.abs(devValue)) });
  };

  const columns: GridColDef<AssetAllocationAsset>[] = [
    {
      field: "commodity",
      headerName: t("Commodity"),
      flex: 1,
      renderCell: ({ row }) => (row.name !== row.commodity ? `${row.commodity} (${row.name})` : row.commodity),
    },
    {
      field: "targetPct",
      headerName: t("Target"),
      headerAlign: "center",
      align: "right",
      minWidth: 90,
      valueFormatter: (value: number) => percentFormatter(value / 100),
    },
    {
      field: "currentPct",
      headerName: t("Current"),
      headerAlign: "center",
      align: "right",
      minWidth: 90,
      valueFormatter: (value: number) => percentFormatter(value / 100),
    },
    {
      field: "value",
      headerName: t("Value"),
      headerAlign: "center",
      align: "right",
      minWidth: 120,
      valueFormatter: (value: number) => currencyFormatter(value),
    },
    {
      field: "devPct",
      headerName: t("Deviation"),
      description: t("Deviation from the target allocation in percentage points"),
      headerAlign: "center",
      align: "right",
      minWidth: 100,
      valueFormatter: (value: number) => `${value >= 0 ? "+" : ""}${percentFormatter(value / 100)}`,
      cellClassName: ({ row }) => (row.over ? "diverged" : ""),
    },
    {
      field: "devValue",
      headerName: t("Rebalance"),
      headerAlign: "center",
      align: "right",
      minWidth: 140,
      valueFormatter: (value: number) => rebalance(value),
    },
  ];

  return (
    <DataGrid
      columns={columns}
      rows={portfolio.assets}
      getRowId={(row) => row.commodity}
      density="compact"
      hideFooter
      disableColumnMenu
      initialState={{
        sorting: {
          sortModel: [{ field: "value", sort: "desc" }],
        },
      }}
      sx={{
        ".MuiDataGrid-cell:not([data-field='commodity'])": {
          fontFamily: '"Fira Mono", monospace',
        },
        ".diverged": {
          color: theme.pnl.loss,
          fontWeight: "bold",
        },
      }}
    />
  );
}
