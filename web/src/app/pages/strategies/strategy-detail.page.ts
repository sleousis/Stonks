import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type {
  StatusChangeView,
  StrategyMetadataView,
  StrategyStatus,
  SurvivalReportView,
} from '../../api/models';
import { StrategiesService } from '../../api/strategies.service';
import { formatDate, formatDateTime } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { promoteThroughGate } from '../../shared/governance';
import { formatMetric, metricLabel } from '../../shared/metrics';
import { HelpTip } from '../../shared/ui/help-tip';
import { PageHeader } from '../../shared/ui/page-header';
import { ErrorState, EmptyState, LoadingState } from '../../shared/ui/states';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { StatusPill } from '../../shared/ui/status-pill';
import { formatParam, shortClassName } from './strategy-format';

type Action = 'promote' | 'shadow' | 'retire';

const ACTION_LABELS: Record<Action, string> = {
  promote: 'Promote to active',
  shadow: 'Move to shadow',
  retire: 'Retire',
};

const TARGET: Record<Action, StrategyStatus> = {
  promote: 'active',
  shadow: 'shadow',
  retire: 'retired',
};

/** Research-card labels for the metadata enums. */
export const ALPHA_FAMILY_LABELS: Record<StrategyMetadataView['alpha_family'], string> = {
  trend: 'Trend',
  reversion: 'Mean reversion',
  carry: 'Carry',
  value: 'Value',
  quality: 'Quality',
  growth: 'Growth',
  sentiment: 'Sentiment',
  data_driven: 'Data-driven',
  benchmark: 'Benchmark',
  other: 'Other',
};

const PREMISE_LABELS: Record<StrategyMetadataView['premise'], string> = {
  trend: 'Trends persist',
  mean_reversion: 'Prices return to a mean',
  none: 'None stated',
};

export interface HistoryEntry {
  id: number;
  when: string;
  from: string | null;
  to: string | null;
  kind: string;
  actor: string;
  reason: string;
  override: boolean;
  golive: boolean | null;
}

/** Newest first, for the timeline. */
export function historyEntries(items: readonly StatusChangeView[]): HistoryEntry[] {
  return [...items]
    .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id)
    .map((h) => ({
      id: h.id,
      when: h.created_at,
      from: h.from_status,
      to: h.to_status,
      kind: h.kind,
      actor: h.actor,
      reason: h.reason,
      override: h.override,
      golive: h.golive_passed,
    }));
}

function metricList(report: SurvivalReportView): { key: string; label: string; value: string }[] {
  return Object.entries(report.metrics).map(([key, value]) => ({
    key,
    label: metricLabel(key),
    value: formatMetric(key, value),
  }));
}

@Component({
  selector: 'app-strategy-detail-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    HelpTip,
    RouterLink,
    PageHeader,
    StatusPill,
    StatusChangeDialog,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  templateUrl: './strategy-detail.page.html',
  styleUrl: './strategy-detail.page.scss',
})
export class StrategyDetailPage {
  private readonly strategiesApi = inject(StrategiesService);
  private readonly toasts = inject(ToastService);
  private readonly dialog = viewChild.required(StatusChangeDialog);

  /** Route param `:id`. */
  readonly id = input.required<string>();

  protected readonly strategy = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.strategiesApi.get(params.id),
  });

  /** Audited status changes: its own panel, so a failure never blanks the page. */
  protected readonly history = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.strategiesApi.history(params.id),
  });
  protected readonly timeline = computed(() =>
    this.history.hasValue() ? historyEntries(this.history.value()) : [],
  );

  protected readonly busy = signal<Action | null>(null);

  protected readonly detail = computed(() =>
    this.strategy.hasValue() ? this.strategy.value() : null,
  );

  protected readonly description = computed(() => {
    const s = this.detail();
    return s ? s.class_path : 'Status, parameters and survival evidence.';
  });

  /** Actions that change the current status, in the order they are offered. */
  protected readonly actions = computed<{ key: Action; label: string }[]>(() => {
    const s = this.detail();
    if (!s) return [];
    return (['promote', 'shadow', 'retire'] as const)
      .filter((key) => TARGET[key] !== s.status)
      .map((key) => ({ key, label: ACTION_LABELS[key] }));
  });

  protected readonly research = computed(() => {
    const m = this.detail()?.metadata;
    if (!m) return null;
    return {
      hypothesis: m.hypothesis.trim(),
      family: ALPHA_FAMILY_LABELS[m.alpha_family] ?? m.alpha_family,
      premise: PREMISE_LABELS[m.premise] ?? m.premise,
      horizon: m.label_horizon_bars,
      history: m.required_history_bars,
    };
  });

  protected readonly params = computed(() => {
    const s = this.detail();
    if (!s) return [];
    return Object.entries(s.params).map(([key, value]) => ({ key, value: formatParam(value) }));
  });

  protected readonly reports = computed(() =>
    (this.detail()?.survival_reports ?? []).map((r) => ({ ...r, metricList: metricList(r) })),
  );

  protected readonly reportSummary = computed(() => {
    const reports = this.detail()?.survival_reports ?? [];
    if (!reports.length) return null;
    const passed = reports.filter((r) => r.passed).length;
    return {
      passed,
      total: reports.length,
      allPassed: passed === reports.length,
      label: `${passed} of ${reports.length} passed`,
    };
  });

  protected readonly shortClass = shortClassName;
  protected readonly date = formatDate;
  protected readonly dateTime = formatDateTime;

  protected async run(action: Action): Promise<void> {
    const s = this.detail();
    if (!s || this.busy()) return;
    const done = action === 'promote' ? await this.promote(s.id) : await this.demote(action, s);
    if (!done) return;
    this.strategy.reload();
    this.history.reload();
  }

  private async promote(id: string): Promise<boolean> {
    const result = await promoteThroughGate({
      id,
      dialog: this.dialog(),
      toasts: this.toasts,
      golive: () => this.strategiesApi.golive(id),
      promote: (body) => this.strategiesApi.promote(id, body, true),
      title: `Promote ${id}?`,
      message: 'It becomes active and places orders from the next tick.',
      confirmLabel: 'Promote',
      busy: (on) => this.busy.set(on ? 'promote' : null),
    });
    if (!result) return false;
    this.toasts.success(`Promoted ${id}.`);
    return true;
  }

  private async demote(action: 'shadow' | 'retire', s: { id: string; status: string }) {
    const retire = action === 'retire';
    const body = await this.dialog().open(
      retire
        ? {
            title: `Retire ${s.id}?`,
            message:
              'It stops trading and stops shadow decisions from the next tick. Its artifacts and reports stay in the registry.',
            confirmLabel: 'Retire',
            tone: 'danger',
            minReason: 1,
          }
        : {
            title: `Move ${s.id} to shadow?`,
            message:
              s.status === 'active'
                ? 'It stops trading from the next tick and keeps making virtual decisions you can compare in Shadow.'
                : 'It makes virtual decisions from the next tick without placing orders.',
            confirmLabel: 'Move to shadow',
            minReason: 1,
          },
    );
    if (!body) return false;
    this.busy.set(action);
    try {
      await (retire
        ? this.strategiesApi.retire(s.id, body)
        : this.strategiesApi.shadow(s.id, body));
      this.toasts.success(retire ? `Retired ${s.id}.` : `Moved ${s.id} to shadow.`);
      return true;
    } catch {
      return false; // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }
}
