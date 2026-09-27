import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { PriceAlertCreate, PriceAlertView, WatchlistView } from '../../api/models';
import { PriceAlertsService } from '../../api/price-alerts.service';
import { SessionService } from '../../core/auth/session.service';
import { ToastService } from '../../core/notify/toast.service';
import { PermissionNote } from '../../shared/ui/permission-note';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { type AlertCondition, CONDITIONS, conditionText, targetText } from './price-alert-text';

type Target = 'ticker' | 'watchlist';

const TARGETS: SegmentOption<Target>[] = [
  { value: 'ticker', label: 'One ticker' },
  { value: 'watchlist', label: 'A watchlist' },
];

/**
 * Make a price alert, or change one. An alert watches one ticker or every
 * ticker of a watchlist, and fires when the price rises above or falls below
 * a level, or moves by a percent either way over a number of days. Editing
 * keeps the target and the condition (make a new alert for another one) and
 * starts the alert fresh from the next price.
 *
 *   <app-price-alert-editor [watchlists]="lists" [editing]="alert" (saved)="reload()" />
 */
@Component({
  selector: 'app-price-alert-editor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, Segmented, PermissionNote],
  template: `
    <section class="panel" aria-labelledby="alert-editor-title">
      <div class="panel-head">
        <h2 id="alert-editor-title">{{ editing() ? 'Change the alert' : 'New price alert' }}</h2>
      </div>
      <form class="panel-body form" novalidate (submit)="$event.preventDefault(); save()">
        @if (editing(); as a) {
          <p class="fixed">
            <span class="muted">Watches</span> {{ fixedTarget() }}.
            <span class="muted">Fires when it</span> {{ conditionLabel(a.condition) }}.
            <span class="hint">To watch something else, make a new alert.</span>
          </p>
        } @else {
          <div class="field">
            <span class="label">Watch</span>
            <app-segmented label="Watch" [options]="targets" [(value)]="target" />
          </div>
          @if (target() === 'ticker') {
            <div class="field">
              <label for="pa-ticker">Ticker</label>
              <input
                id="pa-ticker"
                class="input"
                autocomplete="off"
                autocapitalize="characters"
                placeholder="AAPL.US"
                [value]="ticker()"
                [attr.aria-invalid]="!!errors()['ticker']"
                [attr.aria-describedby]="errors()['ticker'] ? 'pa-ticker-error' : null"
                (input)="ticker.set($any($event.target).value.toUpperCase())"
              />
              @if (errors()['ticker']; as e) {
                <span id="pa-ticker-error" class="hint error">{{ e }}</span>
              }
            </div>
          } @else {
            <div class="field">
              <label for="pa-watchlist">Watchlist</label>
              <select
                id="pa-watchlist"
                class="input"
                [attr.aria-invalid]="!!errors()['watchlist']"
                [attr.aria-describedby]="
                  errors()['watchlist'] ? 'pa-watchlist-error' : 'pa-watchlist-hint'
                "
                (change)="watchlist.set($any($event.target).value)"
              >
                <option value="" [selected]="!watchlist()">Pick a watchlist</option>
                @for (w of watchlists(); track w.id) {
                  <option [value]="w.id" [selected]="w.id === watchlist()">
                    {{ w.name }} ({{ w.tickers.length }})
                  </option>
                }
              </select>
              @if (errors()['watchlist']; as e) {
                <span id="pa-watchlist-error" class="hint error">{{ e }}</span>
              } @else if (watchlists().length === 0) {
                <span id="pa-watchlist-hint" class="hint">
                  You have no watchlists yet. <a routerLink="/watchlists">Make one</a>.
                </span>
              } @else {
                <span id="pa-watchlist-hint" class="hint">
                  Every ticker in the list is checked, also ones you add later.
                </span>
              }
            </div>
          }
          <div class="field">
            <span class="label">Fires when the price</span>
            <app-segmented
              label="Fires when the price"
              [options]="conditions"
              [(value)]="condition"
            />
          </div>
        }

        @if (condition() === 'moves_pct') {
          <div class="form-grid form-grid-2">
            <div class="field">
              <label for="pa-pct">Percent move</label>
              <input
                id="pa-pct"
                class="input num"
                type="number"
                inputmode="decimal"
                min="0"
                max="1000"
                step="any"
                [value]="pct()"
                [attr.aria-invalid]="!!errors()['pct']"
                [attr.aria-describedby]="errors()['pct'] ? 'pa-pct-error' : 'pa-pct-hint'"
                (input)="pct.set($any($event.target).value)"
              />
              @if (errors()['pct']; as e) {
                <span id="pa-pct-error" class="hint error">{{ e }}</span>
              } @else {
                <span id="pa-pct-hint" class="hint">Up or down, for example 8 for 8%.</span>
              }
            </div>
            <div class="field">
              <label for="pa-days">Over how many days</label>
              <input
                id="pa-days"
                class="input num"
                type="number"
                inputmode="numeric"
                min="1"
                max="365"
                step="1"
                [value]="days()"
                [attr.aria-invalid]="!!errors()['days']"
                [attr.aria-describedby]="errors()['days'] ? 'pa-days-error' : null"
                (input)="days.set($any($event.target).value)"
              />
              @if (errors()['days']; as e) {
                <span id="pa-days-error" class="hint error">{{ e }}</span>
              }
            </div>
          </div>
        } @else {
          <div class="field">
            <label for="pa-level">Price level</label>
            <input
              id="pa-level"
              class="input num"
              type="number"
              inputmode="decimal"
              min="0"
              step="any"
              [value]="level()"
              [attr.aria-invalid]="!!errors()['level']"
              [attr.aria-describedby]="errors()['level'] ? 'pa-level-error' : 'pa-level-hint'"
              (input)="level.set($any($event.target).value)"
            />
            @if (errors()['level']; as e) {
              <span id="pa-level-error" class="hint error">{{ e }}</span>
            } @else {
              <span id="pa-level-hint" class="hint">Checked on each day's close.</span>
            }
          </div>
        }

        <div class="field">
          <label for="pa-name">Name <span class="muted">(optional)</span></label>
          <input
            id="pa-name"
            class="input"
            maxlength="80"
            [placeholder]="preview()"
            [value]="name()"
            (input)="name.set($any($event.target).value)"
          />
        </div>

        <div class="actions">
          @if (editing()) {
            <button type="button" class="btn" (click)="cancelled.emit()">Cancel</button>
          }
          <button type="submit" class="btn btn-primary" [disabled]="busy() || !canManage()">
            {{ editing() ? 'Save changes' : 'Save alert' }}
          </button>
          <app-permission-note permission="notifications.manage" />
        </div>
      </form>
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .form {
      display: grid;
      gap: var(--space-4);
    }
    .label {
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .fixed {
      display: grid;
      gap: var(--space-1);
      overflow-wrap: anywhere;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
  `,
})
export class PriceAlertEditor {
  private readonly api = inject(PriceAlertsService);
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);

  readonly watchlists = input<readonly WatchlistView[]>([]);
  /** The alert being changed, or null to make a new one. */
  readonly editing = input<PriceAlertView | null>(null);
  readonly saved = output<PriceAlertView>();
  readonly cancelled = output<void>();

  protected readonly targets = TARGETS;
  protected readonly conditions = CONDITIONS;

  protected readonly target = signal<Target>('ticker');
  protected readonly ticker = signal('');
  protected readonly watchlist = signal('');
  protected readonly condition = signal<AlertCondition>('crosses_above');
  protected readonly level = signal('');
  protected readonly pct = signal('');
  protected readonly days = signal('5');
  protected readonly name = signal('');
  protected readonly busy = signal(false);
  protected readonly tried = signal(false);

  protected readonly canManage = computed(() => this.session.can('notifications.manage'));

  constructor() {
    // Load the alert being edited into the form (and clear it after).
    effect(() => {
      const a = this.editing();
      untracked(() => this.load(a));
    });
  }

  protected readonly fixedTarget = computed(() => {
    const a = this.editing();
    return a ? targetText(a, this.watchlists()) : '';
  });

  protected conditionLabel(c: AlertCondition): string {
    return (CONDITIONS.find((o) => o.value === c)?.label ?? c).toLowerCase();
  }

  /** What the alert will say, as the name's placeholder. */
  protected readonly preview = computed(() =>
    conditionText({
      condition: this.condition(),
      level: Number(this.level()) || 0,
      pct: Number(this.pct()) || 0,
      window_days: Number(this.days()) || 1,
    }),
  );

  protected readonly errors = computed(() => {
    const out: Record<string, string> = {};
    if (!this.tried()) return out;
    if (!this.editing()) {
      if (this.target() === 'ticker' && !this.ticker().trim()) {
        out['ticker'] = 'Enter a ticker, like AAPL.US.';
      }
      if (this.target() === 'watchlist' && !this.watchlist()) {
        out['watchlist'] = 'Pick a watchlist.';
      }
    }
    if (this.condition() === 'moves_pct') {
      const pct = Number(this.pct());
      if (!(pct > 0 && pct <= 1000)) out['pct'] = 'Enter a percent above 0, up to 1000.';
      const days = Number(this.days());
      if (!(Number.isInteger(days) && days >= 1 && days <= 365)) {
        out['days'] = 'Enter whole days from 1 to 365.';
      }
    } else if (!(Number(this.level()) > 0)) {
      out['level'] = 'Enter a price above zero.';
    }
    return out;
  });

  async save(): Promise<void> {
    this.tried.set(true);
    if (Object.keys(this.errors()).length) return;
    const moves = this.condition() === 'moves_pct';
    const thresholds = {
      level: moves ? null : Number(this.level()),
      pct: moves ? Number(this.pct()) : null,
      window_days: moves ? Number(this.days()) : null,
      name: this.name().trim() || null,
    };
    this.busy.set(true);
    try {
      const editing = this.editing();
      let result: PriceAlertView;
      if (editing) {
        result = await this.api.update(editing.id, thresholds);
        this.toasts.success('Saved the alert. It starts fresh from the next price.');
      } else {
        const body: PriceAlertCreate = {
          ...thresholds,
          condition: this.condition(),
          ticker: this.target() === 'ticker' ? this.ticker().trim().toUpperCase() : null,
          watchlist_id: this.target() === 'watchlist' ? this.watchlist() : null,
          enabled: true,
        };
        result = await this.api.create(body);
        this.toasts.success(`Saved the alert: ${conditionText(result)}.`);
        this.load(null);
      }
      this.saved.emit(result);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }

  private load(a: PriceAlertView | null): void {
    this.tried.set(false);
    this.name.set(a?.name ?? '');
    if (!a) {
      this.ticker.set('');
      this.level.set('');
      this.pct.set('');
      this.days.set('5');
      return;
    }
    this.target.set(a.target_kind);
    this.condition.set(a.condition);
    this.level.set(a.level === null ? '' : String(a.level));
    this.pct.set(a.pct === null ? '' : String(a.pct));
    this.days.set(a.window_days === null ? '5' : String(a.window_days));
  }
}
