import { Alert, FormControlLabel, Stack, Switch, Theme, Typography, useTheme } from "@mui/material";
import { DataGrid, GridColDef } from "@mui/x-data-grid";
import { createRoute } from "@tanstack/react-router";
import { EChartsOption } from "echarts";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AssetAllocationAsset,
  AssetAllocationClassReport,
  AssetAllocationClassRow,
  AssetAllocationCommodityReport,
  AssetAllocationPortfolio,
  useAssetAllocation,
} from "../api/asset_allocation";
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

const EXAMPLE_DIRECTIVE = `2010-01-01 custom "fava-extension" "fava_portfolio_returns" "{
  'beangrow_config': 'beangrow.pbtxt',
  'asset_allocation_config': 'asset-allocation.yaml',
}"`;

const EXAMPLE_YAML = `portfolios:
  - name: My Portfolio
    accounts:
      - "Assets:Broker:Investments:"
    commodities:              # per-commodity targets, should sum to 100%
      - commodity: ETF_FOO
        target: 60%
      - commodity: ETF_BAR
        target: 40%
    classes:                  # per-asset-class targets, should sum to 100%
      - asset-class: stocks
        target: 70%
      - asset-class: bonds
        target: 30%`;

const MONO = '"Fira Mono", monospace';

/** shared DataGrid styling so the commodity and asset-class tables render identically */
function gridBaseSx(theme: Theme) {
  return {
    ".MuiDataGrid-cell:not([data-field='commodity']):not([data-field='assetClass'])": {
      fontFamily: MONO,
    },
    ".diverged": {
      color: theme.pnl.loss,
      fontWeight: "bold",
    },
  };
}

const commodityLabel = (commodity: string, name: string) => (name !== commodity ? `${commodity} (${name})` : commodity);

/** returns a formatter turning a signed deviation value into a Buy/Sell suggestion */
function useRebalanceFormatter(currency: string) {
  const { t } = useTranslation();
  const currencyFormatter = useCurrencyFormatter(currency);
  return (value: number) => {
    if (Math.abs(value) < 0.01) {
      return "—";
    }
    // positive deviation => overweight => sell; negative => underweight => buy
    return value > 0
      ? t("Sell {{amount}}", { amount: currencyFormatter(Math.abs(value)) })
      : t("Buy {{amount}}", { amount: currencyFormatter(Math.abs(value)) });
  };
}

function AssetAllocation() {
  const { t } = useTranslation();
  const { targetCurrency } = useToolbarContext();
  const [minimize, setMinimize] = useState(false);
  const { isPending, error, data } = useAssetAllocation({ targetCurrency, minimize });

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
              "Compares the current asset allocation of your portfolios against a target allocation. Point the 'asset_allocation_config' option to a YAML file (shareable with the asset-allocation CLI script):",
            )}
          >
            <pre>{EXAMPLE_DIRECTIVE}</pre>
            <p>{t("Example asset-allocation.yaml:")}</p>
            <pre>{EXAMPLE_YAML}</pre>
            <p>
              {t("Asset classes are read from each commodity's 'asset-class' metadata (hierarchical, ':'-separated).")}
            </p>
          </Panel>
        </DashboardRow>
      </Dashboard>
    );
  }

  const hasClassReport = data.portfolios.some((portfolio) => portfolio.classReport !== null);

  return (
    <Dashboard>
      {hasClassReport && (
        <DashboardRow>
          <FormControlLabel
            control={<Switch checked={minimize} onChange={(e) => setMinimize(e.target.checked)} />}
            label={t("Minimize the number of trades when rebalancing asset classes")}
          />
        </DashboardRow>
      )}
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
      <Stack sx={{ gap: 2 }}>
        {portfolio.unpriced.map((commodity) => (
          <Alert severity="warning" key={commodity}>
            {t("Held commodity {{commodity}} has no known price and is excluded from the valuation.", {
              commodity: commodity,
            })}
          </Alert>
        ))}
        {portfolio.commodityReport && <CommoditySection portfolio={portfolio} report={portfolio.commodityReport} />}
        {portfolio.classReport && <ClassSection portfolio={portfolio} report={portfolio.classReport} />}
      </Stack>
    </Panel>
  );
}

/** shared divergence + target-sum alerts for a sub-report */
function ReportAlerts({
  threshold,
  diverged,
  targetSum,
  targetSumOk,
}: {
  threshold: number;
  diverged: boolean;
  targetSum: number;
  targetSumOk: boolean;
}) {
  const { t } = useTranslation();
  return (
    <>
      {diverged ? (
        <Alert severity="warning">
          {t("Some assets diverge by more than ±{{threshold}}% from the target allocation.", { threshold })}
        </Alert>
      ) : (
        <Alert severity="success">
          {t("All assets are within ±{{threshold}}% of the target allocation.", { threshold })}
        </Alert>
      )}
      {!targetSumOk && (
        <Alert severity="error">
          {t("The target allocation sums to {{sum}}%, expected 100%.", { sum: targetSum })}
        </Alert>
      )}
    </>
  );
}

function AllocationChart({
  labels,
  target,
  current,
  height,
}: {
  labels: string[];
  target: number[];
  current: number[];
  height: string;
}) {
  const { t } = useTranslation();
  const theme = useTheme();
  const percentFormatter = usePercentFormatter();
  // reverse so the first item appears at the top of the (inverted) category axis
  const idx = labels.map((_, i) => i).reverse();
  const pct = (value: number) => percentFormatter(value / 100);

  const option: EChartsOption = {
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      // one bar (Current) plus a Target marker per row; show both values cleanly
      formatter: (params) => {
        const items = Array.isArray(params) ? params : [params];
        const category = items[0]?.name ?? "";
        const lines = items.map((item) => {
          const raw = item.value;
          const value = Array.isArray(raw) ? Number(raw[0]) : Number(raw);
          return `${item.marker ?? ""} ${item.seriesName ?? ""}: ${pct(value)}`;
        });
        return [category, ...lines].join("<br/>");
      },
    },
    legend: { bottom: 0 },
    grid: { left: 120, right: 20, top: 10, bottom: 30 },
    xAxis: {
      type: "value",
      axisLabel: { formatter: (value: number) => pct(value) },
    },
    yAxis: {
      type: "category",
      data: idx.map((i) => labels[i]),
    },
    series: [
      {
        type: "bar",
        name: t("Current"),
        barWidth: "55%",
        data: idx.map((i) => current[i]),
      },
      {
        // target drawn as a thin vertical marker across the bar (bullet-chart style),
        // like Fava's budget bars: the bar shows the current value, the tick the target
        type: "scatter",
        name: t("Target"),
        symbol: "rect",
        symbolSize: [3, 22],
        itemStyle: { color: theme.palette.text.primary },
        z: 3,
        data: labels.map((label, i) => [target[i], label]),
      },
    ],
  };

  return <EChart height={height} option={option} />;
}

function CommoditySection({
  portfolio,
  report,
}: {
  portfolio: AssetAllocationPortfolio;
  report: AssetAllocationCommodityReport;
}) {
  const { t } = useTranslation();
  const currencyFormatter = useCurrencyFormatter(portfolio.currency);
  const height = `${Math.max(120, report.assets.length * 34 + 60)}px`;

  return (
    <Stack sx={{ gap: 1 }}>
      <Typography variant="subtitle1" sx={{ fontWeight: "bold" }}>
        {t("By commodity")}
      </Typography>
      <ReportAlerts threshold={portfolio.threshold} {...report} />
      {report.unconfigured.map((holding) => (
        <Alert severity="warning" key={holding.commodity}>
          {t("Held commodity {{commodity}} ({{value}}) is not part of the target allocation.", {
            commodity: holding.commodity,
            value: currencyFormatter(holding.value),
          })}
        </Alert>
      ))}
      <AllocationChart
        labels={report.assets.map((a) => a.commodity)}
        target={report.assets.map((a) => a.targetPct)}
        current={report.assets.map((a) => a.currentPct)}
        height={height}
      />
      <CommodityTable portfolio={portfolio} report={report} />
    </Stack>
  );
}

function CommodityTable({
  portfolio,
  report,
}: {
  portfolio: AssetAllocationPortfolio;
  report: AssetAllocationCommodityReport;
}) {
  const { t } = useTranslation();
  const theme = useTheme();
  const currencyFormatter = useCurrencyFormatter(portfolio.currency);
  const percentFormatter = usePercentFormatter({ fixed: true });
  const rebalance = useRebalanceFormatter(portfolio.currency);

  const columns: GridColDef<AssetAllocationAsset>[] = [
    {
      field: "commodity",
      headerName: t("Commodity"),
      flex: 1,
      renderCell: ({ row }) => commodityLabel(row.commodity, row.name),
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
      rows={report.assets}
      getRowId={(row) => row.commodity}
      density="compact"
      hideFooter
      disableColumnMenu
      initialState={{
        sorting: {
          sortModel: [{ field: "value", sort: "desc" }],
        },
      }}
      sx={gridBaseSx(theme)}
    />
  );
}

function ClassSection({
  portfolio,
  report,
}: {
  portfolio: AssetAllocationPortfolio;
  report: AssetAllocationClassReport;
}) {
  const { t } = useTranslation();
  const currencyFormatter = useCurrencyFormatter(portfolio.currency);
  const height = `${Math.max(120, report.classes.length * 34 + 60)}px`;

  return (
    <Stack sx={{ gap: 1 }}>
      <Typography variant="subtitle1" sx={{ fontWeight: "bold" }}>
        {t("By asset class")}
      </Typography>
      <ReportAlerts threshold={portfolio.threshold} {...report} />
      {report.unconfigured.map((holding) => (
        <Alert severity="warning" key={holding.commodity}>
          {t("Held commodity {{commodity}} ({{value}}) is excluded from the class allocation: {{reason}}.", {
            commodity: holding.commodity,
            value: currencyFormatter(holding.value),
            reason: holding.reason ?? "",
          })}
        </Alert>
      ))}
      <AllocationChart
        labels={report.classes.map((c) => c.assetClass)}
        target={report.classes.map((c) => c.targetPct)}
        current={report.classes.map((c) => c.currentPct)}
        height={height}
      />
      <ClassTable portfolio={portfolio} report={report} />
    </Stack>
  );
}

function ClassTable({
  portfolio,
  report,
}: {
  portfolio: AssetAllocationPortfolio;
  report: AssetAllocationClassReport;
}) {
  const { t } = useTranslation();
  const theme = useTheme();
  const currencyFormatter = useCurrencyFormatter(portfolio.currency);
  const percentFormatter = usePercentFormatter({ fixed: true });
  const rebalance = useRebalanceFormatter(portfolio.currency);

  // Each row is one asset class; its per-commodity rebalance breakdown is
  // rendered as aligned sub-lines inside the same row. This keeps the table
  // sortable (like the commodity table) while ensuring a column sort always
  // orders by the class-level value, never by an individual commodity.
  const subLine = { opacity: 0.7 };

  const notes = (row: AssetAllocationClassRow) => {
    const lines: string[] = [];
    if (row.noHoldings && row.devValue !== 0) {
      lines.push(t("(no current holdings in this class)"));
    }
    if (row.shortfall > 0) {
      lines.push(
        t("(short {{amount}} — cannot reconcile within the commodity ±threshold)", {
          amount: currencyFormatter(row.shortfall),
        }),
      );
    }
    return lines;
  };

  const columns: GridColDef<AssetAllocationClassRow>[] = [
    {
      field: "assetClass",
      headerName: t("Asset class / Commodity"),
      flex: 1,
      renderCell: ({ row }) => (
        <Stack sx={{ gap: 0.25, py: 0.5 }}>
          <span style={{ fontWeight: "bold" }}>{row.assetClass}</span>
          {row.members.map((member) => (
            <span key={member.commodity} style={{ paddingLeft: 16, ...subLine }}>
              {commodityLabel(member.commodity, member.name)}
            </span>
          ))}
          {notes(row).map((note) => (
            <span key={note} style={{ paddingLeft: 16, fontStyle: "italic", ...subLine }}>
              {note}
            </span>
          ))}
        </Stack>
      ),
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
      renderCell: ({ row }) => (
        <Stack sx={{ gap: 0.25, py: 0.5, width: "100%", alignItems: "flex-end" }}>
          <span>{currencyFormatter(row.value)}</span>
          {row.members.map((member) => (
            <span key={member.commodity} style={subLine}>
              {currencyFormatter(member.value)}
            </span>
          ))}
        </Stack>
      ),
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
      renderCell: ({ row }) => (
        <Stack sx={{ gap: 0.25, py: 0.5, width: "100%", alignItems: "flex-end" }}>
          <span>{rebalance(row.devValue)}</span>
          {row.members.map((member) => (
            <span key={member.commodity} style={subLine}>
              {rebalance(member.tradeValue)}
            </span>
          ))}
        </Stack>
      ),
    },
  ];

  return (
    <DataGrid
      columns={columns}
      rows={report.classes}
      getRowId={(row) => row.assetClass}
      density="compact"
      getRowHeight={() => "auto"}
      hideFooter
      disableColumnMenu
      initialState={{
        sorting: {
          sortModel: [{ field: "value", sort: "desc" }],
        },
      }}
      sx={{
        ...gridBaseSx(theme),
        ".MuiDataGrid-cell": {
          display: "flex",
          alignItems: "flex-start",
        },
      }}
    />
  );
}
