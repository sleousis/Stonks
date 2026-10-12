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

import type { SavedScreenView, ScreenAlertSet, ScreenAlertView } from '../../api/models';
import { ScreenerService } from '../../api/screener.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDate } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

export const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'] as const;

/** "Daily" or "Weekly on Friday". */
export function cadenceLabel(alert: Pick<ScreenAlertView, 'cadence' | 'weekday'>): string {
  if (alert.cadence === 'weekly' && alert.weekday != null) {
    return `Weekly on ${WEEKDAYS[alert.weekday] ?? 'a weekend day'}`;
  }
  return 'Daily';
}

/** The "When" select's value for a stored alert ("" when there is none). */
export function whenValue(alert: ScreenAlertView | null | undefined): string {
  if (!alert) return '';
  return alert.cadence === 'weekly' && alert.weekday != null ? String(alert.weekday) : 'daily';
}

/** What the value of the "When" select means as an alert body. */
export function alertBody(when: string): ScreenAlertSet {
  if (when === 'daily') return { enabled: true, cadence: 'daily', weekday: null };
  return { enabled: true, cadence: 'weekly', weekday: Number(when) };
}

/**
 * Screen alerts (roadmap 23.17): a saved screen runs after each data
 * refresh and notifies you about names that newly match, through your
 * notification channels. Notify only: nothing trades. Shown under your
 * saved screens on the screener page.
 */
@Component({
  selector: 'app-screen-alerts',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PermissionNote, EmptyState, ErrorState, LoadingState],
  template: `
    <section class="page-section" aria-labelledby="alerts-title">
      <header class="section-head">
        <h2 id="alerts-title">Screen alerts</h2>
      </header>
      <div class="section-body body">
        <p class="hint intro">
          A saved screen can tell you when new names start to match. It runs after each data
          refresh. The first run only notes today's matches. Choose where alerts go in
          <a routerLink="/settings">Settings</a>.
        </p>
        @if (alerts.error(); as err) {
          <app-error-state
            title="Could not load your alerts"
            [error]="err"
            (retry)="alerts.reload()"
          />
        } @else if (!alerts.hasValue()) {
          <app-loading-state label="Loading your alerts" [rows]="2" />
        } @else if (screens().length === 0) {
          <app-empty-state
            icon="doc"
            title="Save a screen first"
            message="Then it can alert you when new names match."
          />
        } @else {
          <ul class="alerts">
            @for (s of screens(); track s.id) {
              @let a = byScreen().get(s.id);
              <li class="alert">
                <div class="alert-text">
                  <span class="alert-name">{{ s.name }}</span>
                  <span class="hint">
                    @if (a) {
                      {{ label(a) }}{{ a.enabled ? '' : ', off' }}.
                      @if (a.last_as_of) {
                        Last ran <span class="num">{{ day(a.last_as_of) }}</span
                        >, {{ a.matched }} matched.
                      } @else {
                        Not run yet.
                      }
                    } @else {
                      No alert.
                    }
                  </span>
                  @if (a?.last_error; as e) {
                    <span class="error">The last run failed: {{ e }}</span>
                  }
                </div>
                <div class="alert-actions">
                  <label class="visually-hidden" [for]="'alert-when-' + s.id"
                    >When {{ s.name }} alerts</label
                  >
                  <select
                    class="input"
                    [id]="'alert-when-' + s.id"
                    [disabled]="!canEdit() || busy() === s.id"
                    (change)="setWhen(s, $any($event.target))"
                  >
                    @if (!a) {
                      <option value="" selected>Choose when</option>
                    }
                    <option value="daily" [selected]="a?.cadence === 'daily'">Daily</option>
                    @for (d of weekdays; track d) {
                      <option
                        [value]="$index"
                        [selected]="a?.cadence === 'weekly' && a?.weekday === $index"
                      >
                        Weekly on {{ d }}
                      </option>
                    }
                  </select>
                  @if (a) {
                    <button
                      type="button"
                      class="btn btn-ghost btn-compact"
                      [attr.aria-label]="'Remove the alert on ' + s.name"
                      [disabled]="!canEdit() || busy() === s.id"
                      (click)="remove(s)"
                    >
                      Remove
                    </button>
                  }
                </div>
              </li>
            }
          </ul>
        }
        <app-permission-note permission="notifications.manage" />

        @if (events.hasValue() && events.value().items.length) {
          <h3 class="events-title">New names found</h3>
          <ul class="events">
            @for (e of events.value().items; track e.id) {
              <li>
                <span class="num when">{{ day(e.as_of) }}</span>
                <span class="event-text"
                  ><strong>{{ e.screen_name || e.screen_id }}</strong>
                  <span class="tickers">
                    @for (t of e.tickers; track t) {
                      <span class="ticker-tag">{{ t }}</span>
                    }
                  </span></span
                >
              </li>
            }
          </ul>
        }
      </div>
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    .body {
      display: grid;
      gap: var(--space-4);
    }
    .hint {
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .intro {
      max-width: 68ch;
    }
    .alerts,
    .events {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    /* One row per saved screen: name and state left, the cadence right. */
    .alert {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: var(--space-2) var(--space-4);
      min-height: var(--row-h);
      padding: var(--space-3) 0;
      border-bottom: 1px solid var(--rule);
    }
    .alert:first-child {
      border-top: 1px solid var(--rule);
    }
    .alert-text {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .alert-name {
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    .error {
      color: var(--down);
      font-size: var(--text-sm);
    }
    .alert-actions {
      display: flex;
      align-items: center;
      gap: var(--space-2);
    }
    .alert-actions .input {
      width: auto;
      min-width: 10rem;
    }
    .events-title {
      padding-bottom: var(--space-2);
      border-bottom: 2px solid var(--rule-strong);
      font-size: var(--text-lead);
      font-weight: var(--weight-bold);
    }
    .events li {
      display: grid;
      grid-template-columns: 6.5rem minmax(0, 1fr);
      gap: var(--space-3);
      padding: var(--space-3) 0;
      border-bottom: 1px solid var(--rule);
    }
    .when {
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .event-text {
      display: grid;
      gap: var(--space-1);
      min-width: 0;
    }
    .tickers {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-1);
    }
    @include bp.phone {
      .alert {
        grid-template-columns: minmax(0, 1fr);
      }
      .alert-actions .input {
        flex: 1;
      }
      .events li {
        grid-template-columns: minmax(0, 1fr);
      }
    }
  `,
})
export class ScreenAlerts {
  private readonly api = inject(ScreenerService);
  private readonly session = inject(SessionService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  /** Your saved screens (the page loads them). */
  readonly screens = input.required<readonly SavedScreenView[]>();

  protected readonly weekdays = WEEKDAYS;
  protected readonly busy = signal<string | null>(null);
  protected readonly alerts = resource({ loader: () => this.api.alerts() });
  protected readonly events = resource({ loader: () => this.api.alertEvents({ limit: 10 }) });
  protected readonly byScreen = computed(
    () => new Map((this.alerts.hasValue() ? this.alerts.value() : []).map((a) => [a.screen_id, a])),
  );
  protected readonly canEdit = computed(() => this.session.can('notifications.manage'));

  protected label(a: ScreenAlertView): string {
    return cadenceLabel(a);
  }

  protected day(value: string): string {
    return formatDate(value);
  }

  protected async setWhen(screen: SavedScreenView, select: HTMLSelectElement): Promise<void> {
    const when = select.value;
    if (!when) return;
    this.busy.set(screen.id);
    try {
      const saved = await this.api.setAlert(screen.id, alertBody(when));
      this.toasts.success(`${screen.name} alerts you ${cadenceLabel(saved).toLowerCase()}.`);
      this.alerts.reload();
    } catch {
      // The error interceptor already showed the API's message. Put the
      // select back: its option bindings did not change, so nothing else will.
      select.value = whenValue(this.byScreen().get(screen.id));
    } finally {
      this.busy.set(null);
    }
  }

  protected async remove(screen: SavedScreenView): Promise<void> {
    const ok = await this.confirm.confirm({
      title: `Remove the alert on ${screen.name}?`,
      message: 'The screen stays. A new alert starts again from the names that match then.',
      confirmLabel: 'Remove alert',
    });
    if (!ok) return;
    this.busy.set(screen.id);
    try {
      await this.api.deleteAlert(screen.id);
      this.alerts.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }
}
