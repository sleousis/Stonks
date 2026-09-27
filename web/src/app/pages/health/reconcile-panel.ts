import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import { LiveService } from '../../api/live.service';
import type { DriftItemView, ReconcileReportView } from '../../api/models';
import { formatAgo, formatDateTime } from '../../core/format/format';
import { autoRefresh } from '../../shared/auto-refresh';
import { ErrorState } from '../../shared/ui/states';
import { StatusPill, type PillTone } from '../../shared/ui/status-pill';

const KIND_WORDS: Record<string, string> = {
  sod: 'Start of day',
  submit: 'Before submit',
  eod: 'End of day',
  adhoc: 'By hand',
};

const ITEM_WORDS: Record<string, string> = {
  position_qty: 'Position differs',
  unknown_position: 'Position Stonks never traded',
  unknown_order: 'Order Stonks did not place',
  missing_order: 'Order the broker lost',
  order_state: 'Order state differs',
  unknown_execution: 'Fill with no order',
  unresolved_order: 'Order outcome unknown',
  stuck_order: 'Order still working after the close',
  commission_missing: 'Commission not reported yet',
  stale_order: 'Old order cancelled',
};

export interface ReconcileState {
  status: string;
  label: string;
  tone: PillTone;
}

/** How a report's status reads. */
export function reconcileState(r: ReconcileReportView): ReconcileState {
  switch (r.status) {
    case 'clean':
      return { status: 'ok', label: 'Matches', tone: 'positive' };
    case 'warn':
      return { status: 'warn', label: 'Look at it', tone: 'warn' };
    case 'drift':
      return { status: 'halted', label: 'Drift, buys halted', tone: 'negative' };
    case 'outage':
      return { status: 'skipped', label: 'Broker unreachable', tone: 'warn' };
    default:
      return { status: 'unhealthy', label: 'Broker fault', tone: 'negative' };
  }
}

export function kindWords(kind: string): string {
  return KIND_WORDS[kind] ?? kind;
}

export function itemWords(item: DriftItemView): string {
  return ITEM_WORDS[item.kind] ?? item.kind.replace(/_/g, ' ');
}

/**
 * Health: the latest checks of your live portfolios against their broker.
 * The broker is the source of truth. A difference in Stonks' own positions
 * or orders halts new buys and pauses auto. Your own trades in the same
 * account are shown apart and never count. Hidden while there is no report.
 */
@Component({
  selector: 'app-reconcile-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill, ErrorState],
  template: `
    @if (reports.error(); as err) {
      <section class="panel" aria-labelledby="reconcile-title">
        <div class="panel-head"><h2 id="reconcile-title">Broker reconciliation</h2></div>
        <app-error-state
          title="Could not load the reconcile reports"
          [error]="err"
          (retry)="reports.reload()"
        />
      </section>
    } @else if (reports.hasValue() && reports.value().length) {
      <section class="panel" aria-labelledby="reconcile-title">
        <div class="panel-head">
          <h2 id="reconcile-title">Broker reconciliation</h2>
          @if (driftCount(); as n) {
            <span class="drift num">{{ n }} with drift</span>
          }
        </div>
        <ul class="reports">
          @for (r of reports.value(); track r.id) {
            <li class="report" [attr.data-state]="state(r).status">
              <div class="report-head">
                <h3>{{ kind(r.kind) }}</h3>
                <span class="muted">{{ r.portfolio_id }}</span>
                <app-status-pill
                  [status]="state(r).status"
                  [label]="state(r).label"
                  [tone]="state(r).tone"
                />
                <span class="muted num when">{{ when(r.taken_at) }}</span>
              </div>
              @if (r.detail) {
                <p class="problem" role="note">{{ r.detail }}</p>
              }
              @if (r.items.length) {
                <ul class="items">
                  @for (i of r.items; track i.kind + i.key) {
                    <li [class.material]="i.material">
                      <strong>{{ item(i) }}</strong>
                      <span class="key">{{ i.key }}</span>
                      <span class="muted">{{ i.detail }}</span>
                    </li>
                  }
                </ul>
              }
              @if (r.halt_id) {
                <p class="hint">
                  Halt #{{ r.halt_id }} stops new buys. Fix the cause, then clear the halt with a
                  reason. Auto stays paused until you turn it on again.
                </p>
              }
              @if (externalCount(r); as n) {
                <p class="muted num">
                  {{ n }} of your own holdings or orders in the account, kept apart.
                </p>
              }
            </li>
          }
        </ul>
      </section>
    }
  `,
  styles: `
    .drift {
      color: var(--color-loss);
      font-weight: var(--weight-semibold);
    }
    .reports {
      display: grid;
      gap: var(--space-3);
      margin: 0;
      padding: var(--space-4);
      list-style: none;
    }
    .report {
      display: grid;
      gap: var(--space-2);
      min-width: 0;
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border);
      border-left: 4px solid var(--color-gain);
      border-radius: var(--radius-sm);
      background: var(--color-surface);
    }
    .report[data-state='halted'],
    .report[data-state='unhealthy'] {
      border-left-color: var(--color-loss);
    }
    .report[data-state='warn'],
    .report[data-state='skipped'] {
      border-left-color: var(--color-border-strong);
    }
    .report-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
    }
    h3 {
      font-size: var(--text-md);
    }
    .when {
      margin-left: auto;
    }
    .items {
      display: grid;
      gap: var(--space-1);
      margin: 0;
      padding-left: var(--space-4);
    }
    .items li {
      overflow-wrap: anywhere;
    }
    .items li.material strong {
      color: var(--color-loss);
    }
    .key {
      margin: 0 var(--space-2);
      font-family: var(--font-mono);
    }
    .problem {
      padding: var(--space-2) var(--space-3);
      border-radius: var(--radius-xs);
      background: var(--color-loss-soft);
      overflow-wrap: anywhere;
    }
  `,
})
export class ReconcilePanel {
  private readonly live = inject(LiveService);

  protected readonly reports = resource({ loader: () => this.live.reconcileReports() });
  private readonly auto = autoRefresh(() => [this.reports]);

  protected readonly driftCount = computed(() =>
    this.reports.hasValue() ? this.reports.value().filter((r) => r.status === 'drift').length : 0,
  );

  protected readonly state = reconcileState;
  protected readonly kind = kindWords;
  protected readonly item = itemWords;
  protected readonly when = (at: string) => `${formatAgo(at)}, ${formatDateTime(at)}`;
  protected readonly externalCount = (r: ReconcileReportView) =>
    Object.keys(r.external.positions).length + r.external.orders.length;

  reload(): void {
    this.auto.refresh();
  }
}
