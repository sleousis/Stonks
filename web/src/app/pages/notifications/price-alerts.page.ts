import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import type { PriceAlertView } from '../../api/models';
import { PriceAlertsService } from '../../api/price-alerts.service';
import { WatchlistsService } from '../../api/watchlists.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { NotificationsTabs } from './notifications-tabs';
import { PriceAlertEditor } from './price-alert-editor';
import { PriceAlertEvents } from './price-alert-events';
import { alertTitle, conditionText, targetText } from './price-alert-text';

/**
 * Your price alerts: make one on a ticker or a watchlist, switch it on or
 * off, change its level, delete it, and see when each fired. A firing goes to
 * the feed, push, email and Telegram like any other notification.
 */
@Component({
  selector: 'app-price-alerts-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    NotificationsTabs,
    PriceAlertEditor,
    PriceAlertEvents,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  template: `
    <app-page-header
      title="Price alerts"
      description="Get told when a price crosses a level or moves fast. Alerts check each closing price in the evening."
    />
    <app-notifications-tabs />

    <div class="layout">
      <app-price-alert-editor
        [watchlists]="lists()"
        [editing]="editing()"
        (saved)="afterSave()"
        (cancelled)="editing.set(null)"
      />

      <section class="page-section" aria-labelledby="alerts-title">
        <header class="section-head">
          <h2 id="alerts-title">
            Your alerts
            @if (alerts.hasValue()) {
              <span class="count">{{ alerts.value().length }}</span>
            }
          </h2>
        </header>
        @if (alerts.error(); as err) {
          <app-error-state title="Could not load alerts" [error]="err" (retry)="alerts.reload()" />
        } @else if (!alerts.hasValue()) {
          <app-loading-state label="Loading alerts" [rows]="3" />
        } @else if (alerts.value().length === 0) {
          <app-empty-state
            title="No price alerts yet"
            message="Make one with the form. It fires into your feed and on the channels you turned on in Settings."
          />
        } @else {
          <ul class="alerts">
            @for (a of alerts.value(); track a.id) {
              <li [class.off]="!a.enabled">
                <div class="text">
                  <p class="name">{{ title(a) }}</p>
                  <p class="rule">
                    @if (a.name) {
                      <span>{{ target(a) }}:</span>
                    }
                    {{ condition(a) }}
                  </p>
                </div>
                <div class="controls">
                  <label class="check">
                    <input
                      type="checkbox"
                      [checked]="a.enabled"
                      [disabled]="busy() === a.id || !canManage()"
                      (change)="toggle(a, $any($event.target).checked)"
                    />
                    On<span class="visually-hidden">: {{ title(a) }}</span>
                  </label>
                  <button
                    type="button"
                    class="btn btn-ghost"
                    [disabled]="!canManage()"
                    (click)="editing.set(a)"
                  >
                    Change<span class="visually-hidden">: {{ title(a) }}</span>
                  </button>
                  <button
                    type="button"
                    class="btn btn-ghost"
                    [disabled]="busy() === a.id || !canManage()"
                    (click)="remove(a)"
                  >
                    Delete<span class="visually-hidden">: {{ title(a) }}</span>
                  </button>
                </div>
              </li>
            }
          </ul>
        }
      </section>
    </div>

    <app-price-alert-events
      [alerts]="alerts.hasValue() ? alerts.value() : []"
      [watchlists]="lists()"
      [refresh]="version()"
    />
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-6);
      grid-template-columns: minmax(0, 1fr);
      min-width: 0;
    }
    app-page-header {
      margin-bottom: -28px;
    }
    .layout {
      display: grid;
      gap: var(--section-gap) var(--column-gap);
      min-width: 0;
      @include bp.from-desktop {
        grid-template-columns: minmax(0, 2fr) minmax(0, 3fr);
        align-items: start;
      }
    }
    .alerts {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .alerts li {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2) var(--space-3);
      min-height: var(--row-h-2);
      padding: var(--space-3) 0;
      border-bottom: 1px solid var(--rule);
    }
    .alerts li.off .text {
      color: var(--muted);
    }
    .text {
      display: grid;
      gap: 2px;
      flex: 1 1 14rem;
      min-width: 0;
    }
    .name {
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    .rule {
      font-size: var(--text-sm);
      color: var(--ink-2);
      overflow-wrap: anywhere;
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1);
    }
  `,
})
export class PriceAlertsPage {
  private readonly api = inject(PriceAlertsService);
  private readonly watchlistsApi = inject(WatchlistsService);
  private readonly session = inject(SessionService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  protected readonly alerts = resource({ loader: () => this.api.list() });
  private readonly watchlists = resource({ loader: () => this.watchlistsApi.list(true) });
  protected readonly lists = computed(() =>
    this.watchlists.hasValue() ? this.watchlists.value() : [],
  );
  protected readonly editing = signal<PriceAlertView | null>(null);
  protected readonly busy = signal<string | null>(null);
  protected readonly version = signal(0);
  protected readonly canManage = computed(() => this.session.can('notifications.manage'));

  protected title(a: PriceAlertView): string {
    return alertTitle(a, this.lists());
  }
  protected target(a: PriceAlertView): string {
    return targetText(a, this.lists());
  }
  protected condition(a: PriceAlertView): string {
    return conditionText(a);
  }

  protected afterSave(): void {
    this.editing.set(null);
    this.alerts.reload();
  }

  protected async toggle(a: PriceAlertView, on: boolean): Promise<void> {
    this.busy.set(a.id);
    try {
      await this.api.update(a.id, { enabled: on });
      this.toasts.success(on ? `Turned on ${this.title(a)}.` : `Turned off ${this.title(a)}.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
      this.alerts.reload();
    }
  }

  protected async remove(a: PriceAlertView): Promise<void> {
    const name = this.title(a);
    const ok = await this.confirm.confirm({
      title: `Delete the alert ${name}?`,
      message: 'It stops firing. Its past firings stay in the list below.',
      confirmLabel: 'Delete alert',
      tone: 'danger',
    });
    if (!ok) return;
    this.busy.set(a.id);
    try {
      await this.api.delete(a.id);
      this.toasts.success(`Deleted the alert ${name}.`);
      if (this.editing()?.id === a.id) this.editing.set(null);
      this.alerts.reload();
      this.version.update((v) => v + 1);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }
}
