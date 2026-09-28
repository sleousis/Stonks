import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { ShadowDecisionView, StatusChangeView } from '../../api/models';
import { StrategiesService } from '../../api/strategies.service';
import { formatDate, formatDateTime, formatNumber, formatPercent } from '../../core/format/format';
import { checkRow } from '../../shared/golive-checks';
import { STATUS_WORDS } from '../../shared/governance-labels';
import { splitMetrics } from '../../shared/metrics';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { PrintService } from '../../shared/print.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { MonthlyReturns } from '../../shared/ui/monthly-returns';
import { PageHeader } from '../../shared/ui/page-header';
import { SideTag } from '../../shared/ui/side-tag';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { className } from '../lab/ledger.page';
import { goliveLabel } from './leaderboard.page';
import { curveSeries, curveSummary } from './tearsheet-data';
import { testLabel } from '../../shared/lab-results/survival-tests';
import { strategyDisplayName } from '../../shared/strategy-names';

const NA = 'n/a';

/**
 * One strategy on one page: its paper result (figures, value curve and
 * drawdown, monthly returns), recent model-book trades, the survival
 * verdicts from its lab run, the go-live check and its status history.
 * "Download PDF" prints it through the browser (Save as PDF) with the print
 * stylesheet: content only, on white.
 */
@Component({
  selector: 'app-tearsheet-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    StatTile,
    TimeSeriesChart,
    MonthlyReturns,
    DataTable,
    TableCell,
    SideTag,
    StatusPill,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  templateUrl: './tearsheet.page.html',
  styleUrl: './tearsheet.page.scss',
})
export class TearsheetPage {
  /** Route param `/strategies/:id/tearsheet`. */
  readonly id = input.required<string>();
  private readonly api = inject(StrategiesService);
  private readonly printer = inject(PrintService);

  protected readonly sheet = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.api.tearSheet(params.id),
  });

  protected readonly cls = computed(() =>
    this.sheet.hasValue() ? className(this.sheet.value().strategy.class_path) : '',
  );
  protected readonly statusWord = computed(() =>
    this.sheet.hasValue() ? STATUS_WORDS[this.sheet.value().strategy.status] : '',
  );
  protected readonly tiles = computed(() => {
    if (!this.sheet.hasValue()) return [];
    const p = this.sheet.value().paper;
    const pct = (v: number | null | undefined, signed = false) =>
      v === null || v === undefined ? NA : formatPercent(v, { signed });
    const num = (v: number | null | undefined) =>
      v === null || v === undefined ? NA : formatNumber(v, { digits: 2 });
    return [
      { label: 'Total return', value: pct(p.total_return, true), help: 'total_return' },
      { label: 'CAGR', value: pct(p.cagr, true), help: 'cagr' },
      { label: 'Sharpe', value: num(p.sharpe), help: 'sharpe' },
      { label: 'Sortino', value: num(p.sortino), help: 'sortino' },
      { label: 'Max drawdown', value: pct(p.max_drawdown), help: 'max_drawdown' },
      { label: 'Volatility', value: pct(p.volatility), help: 'volatility' },
      { label: 'Days on paper', value: formatNumber(p.days), help: false as const },
      {
        label: 'Trades',
        value: formatNumber(p.trades + this.sheet.value().book_trades),
        help: false as const,
      },
    ];
  });
  protected readonly series = computed(() =>
    this.sheet.hasValue() ? curveSeries(this.sheet.value().curve) : [],
  );
  protected readonly summary = computed(() =>
    this.sheet.hasValue() ? curveSummary(this.sheet.value().curve) : null,
  );
  /** A name to read, not the registry id (UX-27). */
  protected readonly displayName = computed(() => strategyDisplayName(this.id()));
  protected readonly tests = computed(() =>
    this.sheet.hasValue()
      ? this.sheet.value().strategy.survival_reports.map((r) => ({
          id: r.test_id,
          label: testLabel(r.test_id),
          passed: r.passed,
          notes: r.notes,
          figures: splitMetrics(r.test_id, r.metrics).key,
        }))
      : [],
  );
  protected readonly checks = computed(() => {
    const g = this.sheet.hasValue() ? this.sheet.value().golive : null;
    return g ? g.checks.map((c) => checkRow(c, g.strategy_id)) : [];
  });
  protected readonly golive = computed(() =>
    goliveLabel(this.sheet.hasValue() ? this.sheet.value().golive?.passed : null),
  );
  protected readonly history = computed<StatusChangeView[]>(() =>
    this.sheet.hasValue() ? [...this.sheet.value().status_history].reverse() : [],
  );

  protected readonly tradeColumns: TableColumn<ShadowDecisionView>[] = [
    { key: 'as_of', label: 'Date', format: 'date', mobile: 'title' },
    { key: 'side', label: 'Side', sortable: false },
    { key: 'ticker', label: 'Ticker' },
    { key: 'quantity', label: 'Qty', format: 'number' },
    { key: 'price', label: 'Price', format: 'money' },
    { key: 'status', label: 'Status', mobile: 'hide' },
  ];
  protected readonly tradeKey = (d: ShadowDecisionView) => String(d.id);

  /** The day under the printed title. */
  protected readonly printedOn = computed(() => formatDate(new Date().toISOString()));

  /** "Download PDF": the browser's print dialog, where you pick Save as PDF. */
  protected print(): void {
    void this.printer.print();
  }

  protected day(v: string | null | undefined): string {
    return formatDate(v);
  }

  protected when(v: string): string {
    return formatDateTime(v);
  }

  protected statusName(s: string | null): string {
    return s ? (STATUS_WORDS[s as keyof typeof STATUS_WORDS] ?? s) : 'New';
  }
}
