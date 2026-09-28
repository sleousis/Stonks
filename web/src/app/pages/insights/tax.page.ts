import { DOCUMENT } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { TaxSettingsView } from '../../api/models';
import { TaxService } from '../../api/tax.service';
import { SessionService } from '../../core/auth/session.service';
import { formatDateTime } from '../../core/format/format';
import { ApiError } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { saveFile } from '../../shared/ui/export-button';
import { HelpTip } from '../../shared/ui/help-tip';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import { InsightsNav } from './insights-nav';
import { LotPicks } from './lot-picks';

type Jurisdiction = TaxSettingsView['jurisdiction'];
type LotMethod = TaxSettingsView['lot_method'];

export const JURISDICTIONS: readonly SegmentOption<Jurisdiction>[] = [
  { value: 'us', label: 'United States' },
  { value: 'eu', label: 'European Union' },
  { value: 'uk', label: 'United Kingdom' },
];

export const LOT_METHODS: readonly SegmentOption<LotMethod>[] = [
  { value: 'fifo', label: 'Oldest first (FIFO)' },
  { value: 'specific', label: 'Specific lots' },
];

/** `YYYY-MM-DD` of a date in the browser's time zone. */
export function localDay(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** The last few tax years, this one first. */
export function taxYears(now = new Date(), count = 6): number[] {
  const year = now.getFullYear();
  return Array.from({ length: count }, (_, i) => year - i);
}

const CURRENCY = /^[A-Z]{3}$/;

/**
 * Tax settings of the picked portfolio: base currency, where you file, how
 * sales pick their lots (oldest first or your picks), US wash sales, the
 * picks themselves, the yearly CSVs (realized gains, dividends) and the open
 * lots on a day.
 */
@Component({
  selector: 'app-tax-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    HelpTip,
    PageHeader,
    InsightsNav,
    NoBook,
    Segmented,
    PermissionNote,
    LotPicks,
    LoadingState,
    ErrorState,
  ],
  template: `
    <app-page-header
      title="Tax"
      description="How sales pick their lots, and the yearly files for you or your accountant. Not tax advice."
    />
    <app-insights-nav />

    @if (book() === 'none') {
      <app-no-book message="Tax settings and yearly files show here once you have a portfolio." />
    } @else {
      <div class="page-grid">
        <section class="panel span-7" aria-labelledby="tax-settings-title">
          <div class="panel-head">
            <h2 id="tax-settings-title">Settings</h2>
          </div>
          <div class="panel-body">
            @if (settings.error(); as err) {
              <app-error-state
                title="Could not load the tax settings"
                [error]="err"
                (retry)="settings.reload()"
              />
            } @else if (!settings.hasValue()) {
              <app-loading-state label="Loading the tax settings" [rows]="4" />
            } @else {
              <form class="form-grid" (submit)="$event.preventDefault(); save()" novalidate>
                @if (locked()) {
                  <div class="locked" role="note" id="tax-locked">
                    <p class="locked-title">
                      Base currency and where you file are locked while this portfolio trades real
                      money.
                    </p>
                    <p>
                      They are also your broker account's profile: the account rules and the tax
                      files rely on them, so they cannot change under real positions. To change
                      them, move the portfolio back to Broker paper on its
                      <a [routerLink]="['/profile/live', portfolioId()]">live settings</a> page. The
                      lot method and wash sales can still change.
                    </p>
                  </div>
                }
                <div class="field">
                  <span class="label-row">
                    <label for="tax-base">Base currency</label>
                    <app-help-tip term="base_currency" />
                  </span>
                  <input
                    id="tax-base"
                    class="input num base"
                    maxlength="3"
                    autocomplete="off"
                    autocapitalize="characters"
                    [disabled]="!canManage() || locked()"
                    [attr.aria-invalid]="baseError() ? true : null"
                    [attr.aria-describedby]="baseError() ? 'tax-base-error' : 'tax-base-hint'"
                    [value]="base()"
                    (input)="base.set($any($event.target).value.toUpperCase())"
                  />
                  @if (baseError(); as e) {
                    <span id="tax-base-error" class="error">{{ e }}</span>
                  } @else {
                    <span id="tax-base-hint" class="hint"
                      >Values, profit and loss and the tax files are shown in it. Three letters,
                      like USD or EUR.</span
                    >
                  }
                </div>
                <div class="field">
                  <span class="label" aria-hidden="true">Where you file</span>
                  @if (locked()) {
                    <p class="fixed" aria-describedby="tax-locked">
                      <span class="visually-hidden">Where you file: </span>{{ whereLabel() }}
                    </p>
                  } @else {
                    <app-segmented
                      label="Where you file"
                      [options]="jurisdictions"
                      [(value)]="where"
                    />
                  }
                </div>
                <div class="field">
                  <span class="label-row">
                    <span class="label" aria-hidden="true">Lots a sale closes</span>
                    <app-help-tip term="lot" />
                  </span>
                  <app-segmented
                    label="Lots a sale closes"
                    [options]="methods"
                    [(value)]="method"
                  />
                </div>
                <p class="hint">
                  @if (method() === 'fifo') {
                    A sale closes the oldest shares first.
                  } @else {
                    Pick the buys each sale closes below. Shares you do not pick close oldest first.
                  }
                </p>
                @if (where() === 'us') {
                  <label class="check">
                    <input
                      type="checkbox"
                      [disabled]="!canManage()"
                      [checked]="washSales()"
                      (change)="washSales.set($any($event.target).checked)"
                      aria-describedby="tax-wash-hint"
                    />
                    Wash sale adjustment
                  </label>
                  <app-help-tip term="wash_sale" />
                  <p id="tax-wash-hint" class="hint">
                    A loss is put off when you buy the same ticker within 30 days before or after
                    the sale.
                  </p>
                }
                <div class="actions">
                  <button
                    type="submit"
                    class="btn btn-primary"
                    [disabled]="!canManage() || busy() || !dirty()"
                    [attr.aria-busy]="busy()"
                  >
                    Save settings
                  </button>
                  <app-permission-note permission="portfolio.manage" />
                  @if (settings.value().updated_at; as at) {
                    <span class="muted saved">Last changed {{ when(at) }}</span>
                  }
                </div>
              </form>
            }
          </div>
        </section>

        <section class="panel span-5" aria-labelledby="tax-files-title">
          <div class="panel-head">
            <h2 id="tax-files-title">Tax files</h2>
          </div>
          <div class="panel-body files">
            <div class="field">
              <label for="tax-year">Tax year</label>
              <select
                id="tax-year"
                class="input num"
                [value]="year()"
                (change)="year.set(+$any($event.target).value)"
              >
                @for (y of years; track y) {
                  <option [value]="y">{{ y }}</option>
                }
              </select>
            </div>
            <div class="downloads">
              <button
                type="button"
                class="btn"
                [disabled]="downloading() !== null"
                [attr.aria-busy]="downloading() === 'gains'"
                (click)="download('gains')"
              >
                {{ downloading() === 'gains' ? 'Preparing file' : 'Realized gains CSV' }}
              </button>
              <button
                type="button"
                class="btn"
                [disabled]="downloading() !== null"
                [attr.aria-busy]="downloading() === 'dividends'"
                (click)="download('dividends')"
              >
                {{ downloading() === 'dividends' ? 'Preparing file' : 'Dividends CSV' }}
              </button>
            </div>
            <p class="hint">
              Gains list each lot sold in the year with its cost, proceeds and holding period.
              Dividends list gross, withholding and net. Amounts also come in the base currency.
            </p>
            <div class="field">
              <label for="tax-lots-day">Open lots on</label>
              <input
                id="tax-lots-day"
                class="input num"
                type="date"
                [max]="today"
                [value]="lotsDay()"
                (change)="lotsDay.set($any($event.target).value || today)"
              />
            </div>
            <div class="downloads">
              <button
                type="button"
                class="btn"
                [disabled]="downloading() !== null"
                [attr.aria-busy]="downloading() === 'lots'"
                (click)="downloadLots()"
              >
                {{ downloading() === 'lots' ? 'Preparing file' : 'Open lots CSV' }}
              </button>
            </div>
            <p class="hint">
              Open lots list what you still hold, lot by lot: cost basis
              <app-help-tip term="cost_basis" />, days held, short or long term
              <app-help-tip term="long_term" /> and the day it turns long term, and the gain at the
              latest close.
            </p>
          </div>
        </section>

        <section class="panel span-12" aria-labelledby="tax-lots-title">
          <div class="panel-head">
            <h2 id="tax-lots-title">Specific lots <app-help-tip term="specific_lots" /></h2>
          </div>
          <div class="panel-body">
            @if (settings.hasValue() && settings.value().lot_method === 'specific') {
              <app-lot-picks [portfolioId]="portfolioId()" [canManage]="canManage()" />
            } @else {
              <p class="muted">
                Sales close their oldest shares first. Save "Specific lots" above to pick the buys
                each sale closes.
              </p>
            }
          </div>
        </section>
      </div>
    }
  `,
  styles: `
    .base {
      max-width: 8rem;
      text-transform: uppercase;
    }
    .actions,
    .downloads {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
    .saved {
      font-size: var(--text-xs);
    }
    .files {
      display: grid;
      gap: var(--space-3);
    }
    .field select {
      max-width: 10rem;
    }
    .label-row {
      display: inline-flex;
      align-items: center;
      gap: var(--space-1);
    }
    .label {
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .fixed {
      margin: 0;
      color: var(--color-ink-2);
    }
    .locked {
      display: grid;
      gap: var(--space-1);
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-sm);
      background: var(--color-warn-soft);
      font-size: var(--text-sm);
    }
    .locked-title {
      font-weight: var(--weight-semibold);
    }
  `,
})
export class TaxPage {
  private readonly tax = inject(TaxService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);
  private readonly doc = inject(DOCUMENT);

  protected readonly book = computed(() => bookState(this.ctx));
  protected readonly portfolioId = computed(() => this.ctx.selectedId());
  protected readonly canManage = computed(() => this.session.can('portfolio.manage'));

  protected readonly settings = resource({
    params: () => (this.book() === 'ready' ? { portfolio: this.portfolioId() } : undefined),
    loader: () => this.tax.settings(),
  });
  private readonly loaded = computed(() =>
    this.settings.hasValue() ? this.settings.value() : undefined,
  );

  protected readonly base = linkedSignal(() => this.loaded()?.base_currency ?? '');
  protected readonly where = linkedSignal<Jurisdiction>(() => this.loaded()?.jurisdiction ?? 'us');
  protected readonly method = linkedSignal<LotMethod>(() => this.loaded()?.lot_method ?? 'fifo');
  protected readonly washSales = linkedSignal(() => this.loaded()?.wash_sales ?? true);
  /** Real money: base currency and jurisdiction cannot change (the server refuses). */
  protected readonly locked = computed(() => this.loaded()?.locked ?? false);
  protected readonly whereLabel = computed(
    () => JURISDICTIONS.find((j) => j.value === this.where())?.label ?? this.where(),
  );
  protected readonly baseError = signal<string | null>(null);
  protected readonly busy = signal(false);
  protected readonly dirty = computed(() => {
    const s = this.loaded();
    if (!s) return false;
    return (
      this.base().trim() !== s.base_currency ||
      this.where() !== s.jurisdiction ||
      this.method() !== s.lot_method ||
      this.washSales() !== s.wash_sales
    );
  });

  protected readonly jurisdictions = JURISDICTIONS;
  protected readonly methods = LOT_METHODS;
  protected readonly years = taxYears();
  protected readonly year = signal(this.years[0]);
  protected readonly downloading = signal<'gains' | 'dividends' | 'lots' | null>(null);
  /** Today as YYYY-MM-DD in local time: the open-lot report's default and latest day. */
  protected readonly today = localDay(new Date());
  protected readonly lotsDay = signal(this.today);
  protected readonly when = (at: string) => formatDateTime(at);

  protected async save(): Promise<void> {
    const base = this.base().trim().toUpperCase();
    if (!CURRENCY.test(base)) {
      this.baseError.set('Enter a three-letter currency code, like USD.');
      return;
    }
    this.baseError.set(null);
    this.busy.set(true);
    try {
      await this.tax.updateSettings({
        base_currency: base,
        jurisdiction: this.where(),
        lot_method: this.method(),
        wash_sales: this.washSales(),
      });
      this.toasts.success('Saved the tax settings.');
      this.settings.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }

  protected download(kind: 'gains' | 'dividends'): Promise<void> {
    const year = this.year();
    return this.save_(kind, `stonks-tax-${kind}-${year}.csv`, () => this.tax.download(kind, year));
  }

  protected downloadLots(): Promise<void> {
    const day = this.lotsDay();
    return this.save_('lots', `stonks-tax-lots-${day}.csv`, () => this.tax.openLots(day));
  }

  private async save_(
    kind: 'gains' | 'dividends' | 'lots',
    filename: string,
    fetch: () => Promise<Blob>,
  ): Promise<void> {
    this.downloading.set(kind);
    try {
      saveFile(this.doc, { blob: await fetch(), filename });
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'The file could not be made.';
      this.toasts.error(message, 'Download failed');
    } finally {
      this.downloading.set(null);
    }
  }
}
