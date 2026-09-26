import {
  ChangeDetectionStrategy,
  Component,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';

import { AlertsService } from '../../api/alerts.service';
import { formatAgo, formatDateTime } from '../../core/format/format';
import { EmptyState, ErrorState, LoadingState } from './states';
import { type PillTone, StatusPill } from './status-pill';

const LEVELS: Record<string, { tone: PillTone; label: string }> = {
  info: { tone: 'info', label: 'Info' },
  warning: { tone: 'warn', label: 'Warning' },
  warn: { tone: 'warn', label: 'Warning' },
  error: { tone: 'negative', label: 'Error' },
  critical: { tone: 'negative', label: 'Critical' },
};

/** Tone (shape and colour) and word for an alert or notification level. */
export function levelPill(level: string | null | undefined): { tone: PillTone; label: string } {
  const key = (level ?? '').toLowerCase();
  return LEVELS[key] ?? { tone: 'neutral', label: key ? key[0].toUpperCase() + key.slice(1) : 'Info' };
}

/**
 * Recent system alerts (data gaps, failed runs, broker trouble), newest
 * first, one server page at a time. Made for the Health page:
 *
 *   <app-alerts-panel />
 */
@Component({
  selector: 'app-alerts-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill, LoadingState, EmptyState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="alerts-title">
      <div class="panel-head">
        <h2 id="alerts-title">System alerts</h2>
      </div>
      @if (alerts.error(); as err) {
        <app-error-state title="Could not load alerts" [error]="err" (retry)="alerts.reload()" />
      } @else if (!alerts.hasValue()) {
        <app-loading-state label="Loading alerts" [rows]="4" />
      } @else if (alerts.value().items.length === 0) {
        <app-empty-state
          title="No alerts"
          message="Problems the server notices, like missing data, a failed run or a broker it cannot reach, show here."
        />
      } @else {
        @let page = alerts.value();
        <ul class="alerts">
          @for (a of page.items; track a.id) {
            <li>
              <div class="top">
                <app-status-pill
                  [status]="a.level"
                  [tone]="pill(a.level).tone"
                  [label]="pill(a.level).label"
                />
                <time class="when muted" [attr.datetime]="a.created_at" [title]="exact(a.created_at)">
                  {{ ago(a.created_at) }}
                </time>
              </div>
              <p class="title">{{ a.title }}</p>
              @if (a.message) {
                <p class="message">{{ a.message }}</p>
              }
            </li>
          }
        </ul>
        @if (page.total > page.items.length) {
          <nav class="pager" aria-label="Alert pages">
            <button
              type="button"
              class="btn"
              [disabled]="page.offset === 0"
              (click)="offset.set(max(0, page.offset - limit()))"
            >
              Newer
            </button>
            <span class="muted range"
              >{{ page.offset + 1 }} to {{ page.offset + page.items.length }} of
              {{ page.total }}</span
            >
            <button
              type="button"
              class="btn"
              [disabled]="page.offset + page.items.length >= page.total"
              (click)="offset.set(page.offset + limit())"
            >
              Older
            </button>
          </nav>
        }
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .alerts {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .alerts li {
      display: grid;
      gap: var(--space-1);
      padding: var(--space-3) var(--space-4);
      border-bottom: 1px solid var(--color-border);
      min-width: 0;
    }
    .top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
    }
    .when {
      font-size: var(--text-xs);
      white-space: nowrap;
    }
    .title {
      font-weight: var(--weight-medium);
      overflow-wrap: anywhere;
    }
    .message {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .pager {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
    }
    .range {
      font-size: var(--text-sm);
    }
  `,
})
export class AlertsPanel {
  private readonly api = inject(AlertsService);

  /** Alerts per page. */
  readonly limit = input(20);
  protected readonly offset = signal(0);
  protected readonly alerts = resource({
    params: () => ({ limit: this.limit(), offset: this.offset() }),
    loader: ({ params }) => this.api.list(params),
  });
  protected readonly max = Math.max;
  protected readonly pill = levelPill;

  protected ago(value: string): string {
    return formatAgo(value);
  }

  protected exact(value: string): string {
    return formatDateTime(value);
  }
}
