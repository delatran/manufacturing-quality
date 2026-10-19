import type { BackendScope, BackendMetrics, BackendFinancialMetrics } from './generated-scope';
import { initialBackendScope } from './generated-scope';
export type Filters = BackendScope;
export const initialFilters: Filters = initialBackendScope;
export type Metrics = BackendMetrics;
export type ProductMetric = Metrics & {
  product_id: number;
  product_name: string;
  production_line: string;
  category: string;
  rank: number;
  cumulative_share_pct: number | null;
};
export type ProductIntensityMetric = Omit<ProductMetric, 'cumulative_share_pct'> & {
  intensity_available: boolean;
};
export type DashboardData = {
  context: {
    context_id: string;
    snapshot_id: string;
    filters: Filters;
    source_sha256: string;
    master_sha256: string;
    code_sha256: string;
    kpi_version: string;
    fetched_at: string;
    dataset_version?: string;
    policy_version?: string;
    scenario?: string;
  };
  options: Record<string, string[]>;
  evidence: {
    execution_id: string;
    code_sha256: string;
    result_sha256: string;
    numeric_values?: Record<string, unknown>;
    [key: string]: unknown;
  };
  links: {
    records: string;
    evidence: string;
    export_csv: string;
    export_zip: string;
    brief?: string;
    quality_workbook?: string;
  };
  quality_workbook_scope?: {
    kind: string;
    filters_applied: boolean;
    description: string;
  };
  status: {
    stale: boolean;
    last_error: string | null;
    last_attempt_at: string;
  };
  quality_scope: {
    kind: string;
    filters_applied: boolean;
    description: string;
  };
  analysis: {
    kpis: Metrics;
    monthly: (Metrics & {
      month: string;
      label: string;
      severity_counts: Record<string, number>;
    })[];
    trend: { interpretation: string; change_pct: number | null };
    period_halves?: PeriodHalves;
    lines: (Metrics & { production_line: string; product_count: number })[];
    line_category?: (Metrics & { production_line: string; category: string })[];
    category_summary?: (Metrics & {
      category: string;
      product_count: number;
      production_lines: string[];
      eligible_events_per_cohort_product: number | null;
    })[];
    shared_category_controls?: SharedCategoryControl[];
    line_cost_comparison?: LineCostComparison[];
    line_comparison_note?: string;
    location_severity: {
      locations: string[];
      severities: string[];
      location_totals: {
        location: string;
        event_count: number;
        critical_count: number;
        critical_share_pct: number | null;
        share_pct: number | null;
      }[];
      cells: {
        location: string;
        severity: string;
        count: number;
        row_pct: number | null;
      }[];
      test: {
        available: boolean;
        interpretation: string;
        p_value: number | null;
        cramers_v: number | null;
        reliable: boolean;
      };
    };
    products: {
      top: ProductMetric[];
      all: ProductMetric[];
      intensity_top?: ProductIntensityMetric[];
      intensity_all?: ProductIntensityMetric[];
      cost_top: ProductMetric[];
      pareto_product_count_80pct: number | null;
      cost_pareto_product_count_80pct: number | null;
    };
    costs: {
      by_type: CostGroup[];
      by_severity: CostGroup[];
      by_location: CostGroup[];
    };
    cost_distribution?: {
      unit: string;
      event_count: number;
      cost_known_count: number;
      cost_unknown_count: number;
      cost_coverage_pct: number | null;
      quantile_method: string;
      quantiles: Record<'min' | 'p25' | 'median' | 'p75' | 'p90' | 'p95' | 'max', number | null>;
      histogram: {
        lower: number;
        upper: number | null;
        label: string;
        count: number;
        known_cost: number;
        share_of_known_cost_events_pct: number | null;
      }[];
      interpretation: string;
    };
    inspection_methods?: {
      event_count: number;
      rows: (BackendFinancialMetrics & {
        inspection_method: string;
        event_count: number;
        event_share_pct: number | null;
        critical_count: number;
        critical_share_pct: number | null;
        known_cost_share_pct: number | null;
        severity_counts: Record<string, number>;
      })[];
      interpretation: string;
    };
    decomposition?: Record<
      string,
      {
        dimension: string;
        baseline: { scope: Filters; kpis: Metrics };
        comparison: { scope: Filters; kpis: Metrics };
        event_count_change: number;
        known_repair_cost_change: number;
        reference_intensity_change: number | null;
        rows: {
          name: string;
          baseline: Metrics;
          comparison: Metrics;
          event_count_change: number;
          critical_count_change: number;
          known_repair_cost_change: number;
          contribution_pct_of_event_change: number | null;
          reference_intensity_change_contribution: number | null;
        }[];
      }
    >;
    calendar_coverage?: {
      calendar_days: number;
      days_with_events: number;
      days_without_events: number;
      max_observed_day_of_month: number | null;
      selected_month_count: number;
      months_with_events: number;
    };
    anomalies: {
      method: string;
      enabled: boolean;
      baseline_daily_mean: number;
      upper_reference_limit: number | null;
      baseline_count: number;
      candidates: {
        date: string;
        count: number;
        upper_reference_limit: number;
      }[];
      daily: {
        date: string;
        count: number;
        candidate: boolean;
        phase: string;
      }[];
      limitations: string[];
    };
    recommendations: {
      priority: number;
      kind?: string;
      title: string;
      evidence: string;
      action: string;
      owner: string;
      success_measure: string;
      focus?: {
        dimension: 'defect_location' | 'production_line' | 'defect_type' | 'category';
        value: string;
      };
      filters?: Partial<Filters>;
      metrics?: Record<string, string | number | null>;
    }[];
    quality: {
      raw_event_count: number;
      in_scope_event_count: number;
      quarantined_count: number;
      exact_duplicate_count: number;
      normalized_duplicate_count: number;
      complementary_duplicate_count: number;
      unmatched_event_count: number;
      unknown_cost_count: number;
      corrected_source_rows: number;
      flagged_in_scope_event_count: number;
      reconciliation: Record<string, number | boolean>;
      counting_note: string;
      audit_stage_note: string;
      issues: Record<string, unknown>[];
      temporal_coverage: {
        calendar_days: number;
        days_without_events: number;
        days_with_events: number;
      };
    };
    definitions: Record<string, string>;
    metadata: {
      limitations: string[];
      reference_output_note: string;
      fetched_at: string;
      stale: boolean;
      last_error: string | null;
      [key: string]: unknown;
    };
  };
};
export type CostGroup = BackendFinancialMetrics & {
  event_count: number;
  name: string;
  known_cost_share_pct: number | null;
};
type PeriodHalf = {
  months: string[];
  event_count: number;
  critical_count: number;
  known_repair_cost: number;
  cost_known_count: number;
  average_known_repair_cost: number | null;
};
export type PeriodHalves =
  | { enabled: false; reason: string }
  | {
      enabled: true;
      first: PeriodHalf;
      second: PeriodHalf;
      event_count_change: number;
      event_count_change_pct: number | null;
      critical_count_change_pct: number | null;
      known_repair_cost_change_pct: number | null;
    };
export type SharedCategoryControl = {
  category: string;
  line_count: number;
  lines_compared: number;
  lines_above_rest_of_line: number;
  min_intensity_ratio_to_rest_of_line: number | null;
  max_intensity_ratio_to_rest_of_line: number | null;
  lines: {
    production_line: string;
    event_count: number;
    eligible_event_count: number;
    reference_output: number | null;
    reference_intensity_per_1000: number | null;
    rest_of_line_categories: string[];
    rest_of_line_eligible_event_count: number | null;
    rest_of_line_reference_output: number | null;
    rest_of_line_reference_intensity_per_1000: number | null;
    intensity_ratio_to_rest_of_line: number | null;
    average_known_repair_cost: number | null;
    cost_known_count: number;
    same_category_other_lines_average_known_repair_cost: number | null;
    same_category_other_lines_cost_known_count: number;
    cost_ratio_to_same_category_other_lines: number | null;
  }[];
};
export type LineCostComparison = {
  production_line: string;
  event_count: number;
  known_repair_cost: number;
  cost_known_count: number;
  cost_coverage_pct: number | null;
  average_known_repair_cost: number | null;
  other_lines_average_known_repair_cost: number | null;
  other_lines_cost_known_count: number;
  ratio_to_other_lines: number | null;
  known_cost_share_pct: number | null;
  known_cost_event_share_pct: number | null;
};
export type ScenarioComparison = {
  canonical: DashboardData;
  selected: DashboardData;
  comparison: {
    scenario: string;
    metric_deltas: Record<
      string,
      { canonical: number | null; selected: number | null; delta: number | null }
    >;
    priority_order_changed: boolean;
    rankings: Record<string, { canonical: string[]; selected: string[]; changed: boolean }>;
    limitations: string[];
  };
};
export type DefectRecord = {
  defect_id: number;
  defect_date: string;
  product_id: number;
  product_name: string;
  production_line: string;
  severity: string;
  defect_type: string;
  defect_location: string;
  inspection_method: string;
  repair_cost: number | null;
  cost_status: string;
  source_row: number;
};
export type RecordsData = {
  rows: DefectRecord[];
  total: number;
  page: number;
  page_size: number;
  page_count: number;
  [key: string]: unknown;
};
