import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { StrategyDetail, StrategyStatus, SurvivalReportView } from '../../api/models';
import { StrategiesService } from '../../api/strategies.service';
import { ConfirmService, type ConfirmOptions } from '../../core/confirm/confirm.service';
import { formatDate, formatDateTime, formatNumber } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PageHeader } from '../../shared/ui/page-header';
import { ErrorState, EmptyState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { formatParam, shortClassName } from './strategy-format';

type Action = 'promote' | 'shadow' | 'retire';

interface ActionSpec {
  /** Button text. */
  label: string;
  /** Past tense for the toast. */
  done: string;
  confirm: (s: StrategyDetail) => ConfirmOptions;
  call: (api: StrategiesService, id: string) => Promise<unknown>;
}

const ACTIONS: Record<Action, ActionSpec> = {
  promote: {
    label: 'Promote to active',
    done: 'Promoted',
    confirm: (s) => ({
      title: `Promote ${s.id}?`,
      message:
        'It becomes active and trades from the next tick. Check the go-live gate first if it has been in shadow.',
      confirmLabel: 'Promote',
      typedConfirmation: s.id,
    }),
    call: (api, id) => api.promote(id),
  },
  shadow: {
    label: 'Move to shadow',
    done: 'Moved to shadow',
    confirm: (s) => ({
      title: `Move ${s.id} to shadow?`,
      message:
        s.status === 'active'
          ? 'It stops trading from the next tick and keeps making virtual decisions you can compare in Shadow.'
          : 'It makes virtual decisions from the next tick without placing orders.',
      confirmLabel: 'Move to shadow',
    }),
    call: (api, id) => api.shadow(id),
  },
  retire: {
    label: 'Retire',
    done: 'Retired',
    confirm: (s) => ({
      title: `Retire ${s.id}?`,
      message:
        'It stops trading and stops shadow decisions from the next tick. Its artifacts and reports stay in the registry.',
      confirmLabel: 'Retire',
      tone: 'danger',
    }),
    call: (api, id) => api.retire(id),
  },
};

const TARGET: Record<Action, StrategyStatus> = {
  promote: 'active',
  shadow: 'shadow',
  retire: 'retired',
};

function metricList(report: SurvivalReportView): { key: string; value: string }[] {
  return Object.entries(report.metrics).map(([key, value]) => ({
    key,
    value: formatNumber(value, { digits: 4 }),
  }));
}

@Component({
  selector: 'app-strategy-detail-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PageHeader, StatusPill, LoadingState, EmptyState, ErrorState],
  templateUrl: './strategy-detail.page.html',
  styleUrl: './strategy-detail.page.scss',
})
export class StrategyDetailPage {
  private readonly strategiesApi = inject(StrategiesService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  /** Route param `:id`. */
  readonly id = input.required<string>();

  protected readonly strategy = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.strategiesApi.get(params.id),
  });

  protected readonly busy = signal<Action | null>(null);

  protected readonly detail = computed(() =>
    this.strategy.hasValue() ? this.strategy.value() : null,
  );

  protected readonly description = computed(() => {
    const s = this.detail();
    return s ? s.class_path : 'Status, parameters and survival evidence.';
  });

  /** Actions that change the current status, in the order they are offered. */
  protected readonly actions = computed<{ key: Action; spec: ActionSpec }[]>(() => {
    const s = this.detail();
    if (!s) return [];
    return (['promote', 'shadow', 'retire'] as const)
      .filter((key) => TARGET[key] !== s.status)
      .map((key) => ({ key, spec: ACTIONS[key] }));
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
    const spec = ACTIONS[action];
    if (!(await this.confirm.confirm(spec.confirm(s)))) return;
    this.busy.set(action);
    try {
      await spec.call(this.strategiesApi, s.id);
      this.toasts.success(`${spec.done} ${s.id}.`);
      this.strategy.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }
}
