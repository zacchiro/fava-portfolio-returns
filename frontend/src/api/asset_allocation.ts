import { useQuery, UseQueryResult } from "@tanstack/react-query";
import { useFavaFilterSearchParams } from "../routes/__root";
import { fetchJSON } from "./api";

interface AssetAllocationRequest {
  targetCurrency: string;
}

export interface AssetAllocationAsset {
  commodity: string;
  name: string;
  /** target allocation in percent */
  targetPct: number;
  /** current allocation in percent */
  currentPct: number;
  /** current market value in the target currency */
  value: number;
  /** deviation from target in percentage points (positive => overweight) */
  devPct: number;
  /** deviation from target in the target currency (positive => overweight) */
  devValue: number;
  /** whether the deviation exceeds the configured threshold */
  over: boolean;
}

export interface AssetAllocationHolding {
  commodity: string;
  name: string;
  value: number;
}

export interface AssetAllocationPortfolio {
  name: string;
  /** account regexes of this portfolio */
  accounts: string[];
  currency: string;
  totalValue: number;
  /** divergence threshold in percentage points */
  threshold: number;
  /** whether any asset diverges beyond the threshold */
  diverged: boolean;
  assets: AssetAllocationAsset[];
  /** held commodities that are not part of the target allocation */
  unconfigured: AssetAllocationHolding[];
  /** held commodities that could not be valued (no known price) */
  unpriced: string[];
  /** sum of the target allocation, should be 100% */
  targetSum: number;
  targetSumOk: boolean;
}

export interface AssetAllocationResponse {
  portfolios: AssetAllocationPortfolio[];
}

export function useAssetAllocation(request: AssetAllocationRequest): UseQueryResult<AssetAllocationResponse> {
  const params = useFavaFilterSearchParams();
  params.set("currency", request.targetCurrency);
  const url = `asset_allocation?${params}`;

  return useQuery({
    queryKey: [url],
    queryFn: () => fetchJSON<AssetAllocationResponse>(url),
  });
}
