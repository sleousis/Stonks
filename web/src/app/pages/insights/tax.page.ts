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

import type { TaxSettingsView } from '../../api/models';
import { TaxService } from '../../api/tax.service';
import { SessionService } from '../../core/auth/session.service';
import { formatDateTime } from '../../core/format/format';
import { ApiError } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { saveFile } from '../../shared/ui/export-button';
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

/** The last few tax years, this one first. */
export function taxYears(now = new Date(), count = 6): number[] {
  const year = now.getFullYear();
  return Array.from({ length: count }, (_, i) => year - i);
}

const CURRENCY = /^[A-Z]{3}$/;

/**
 * Tax settings of the picked portfolio: base currency, where you file, how
 * sales pick their lots (oldest first or your picks), US wash sales, the
 * picks themselves, and the yearly CSVs (realized gains, dividends).
 */
@Component({
  selector: 'app-tax-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
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
                <div class="field">
                  <label for="tax-base">Base currency</label>
                  <input
                    id="tax-base"
                    class="input num base"
                    maxlength="3"
                    autocomplete="off"
                    autocapitalize="characters"
                    [disabled]="!canManage()"
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
                <app-segmented label="Where you file" [options]="jurisdictions" [(value)]="where" />
                <app-segmented label="Lots a sale closes" [options]="methods" [(value)]="method" />
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
            <h2 id="tax-files-title">Yearly files</h2>
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
          </div>
        </section>

        <section class="panel span-12" aria-labelledby="tax-lots-title">
          <div class="panel-head">
            <h2 id="tax-lots-title">Specific lots</h2>
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
  protected readonly downloading = signal<'gains' | 'dividends' | null>(null);
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

  protected async download(kind: 'gains' | 'dividends'): Promise<void> {
    const year = this.year();
    this.downloading.set(kind);
    try {
      const blob = await this.tax.download(kind, year);
      saveFile(this.doc, { blob, filename: `stonks-tax-${kind}-${year}.csv` });
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'The file could not be made.';
      this.toasts.error(message, 'Download failed');
    } finally {
      this.downloading.set(null);
    }
  }
}
