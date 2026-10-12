import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { type PortfolioRef, PortfoliosService } from '../../api/portfolios.service';
import { atBroker } from '../../shared/live-stages';
import { SessionService } from '../../core/auth/session.service';
import { formatMoney } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PermissionNote } from '../../shared/ui/permission-note';
import { Sheet } from '../../shared/ui/sheet';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

const NAME_MAX = 80;

/**
 * Profile: your portfolios with their PAPER or LIVE stamp, Rename, and a
 * form that opens a new paper portfolio. The list is the portfolio context's
 * (the same one the picker shows), read again after each change.
 */
@Component({
  selector: 'app-portfolios-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ModeStamp, PermissionNote, Sheet, EmptyState, ErrorState, LoadingState],
  template: `
    <section class="page-section" aria-labelledby="portfolios-title">
      <div class="section-head">
        <h2 id="portfolios-title">
          Your portfolios
          @if (ctx.state() === 'ready') {
            <span class="count">{{ books().length }}</span>
          }
        </h2>
      </div>
      @if (ctx.state() === 'failed') {
        <app-error-state
          title="Could not load your portfolios"
          [error]="loadError"
          (retry)="reload()"
        />
      } @else if (ctx.state() === 'idle' || ctx.state() === 'loading') {
        <app-loading-state label="Loading your portfolios" [rows]="2" />
      } @else {
        @if (books().length === 0) {
          <app-empty-state
            compact
            title="No portfolio yet"
            message="Open a paper portfolio below, then follow a strategy on it."
          />
        } @else {
          <ul class="books">
            @for (p of books(); track p.id) {
              <li>
                <app-mode-stamp [live]="p.trading === 'live'" [stage]="p.live_stage" />
                <span class="name">
                  {{ p.name }}
                  @if (p.is_default) {
                    <span class="muted default">Default</span>
                  }
                </span>
                <span class="book-actions">
                  @if (atBroker(p)) {
                    <a
                      class="btn btn-compact"
                      [routerLink]="['/profile/live', p.id]"
                      [attr.aria-label]="'Real-money settings of ' + p.name"
                      >Real-money settings</a
                    >
                  }
                  <button
                    type="button"
                    class="btn btn-ghost btn-compact"
                    [disabled]="!canManage()"
                    [attr.aria-label]="'Rename ' + p.name"
                    (click)="openRename(p)"
                  >
                    Rename
                  </button>
                </span>
              </li>
            }
          </ul>
        }

        <form
          class="new"
          aria-labelledby="new-portfolio-title"
          (submit)="$event.preventDefault(); create()"
          novalidate
        >
          <div class="new-text">
            <h3 id="new-portfolio-title">New paper portfolio</h3>
            <p class="muted">Practice money only. Follow a strategy on it to trade it.</p>
          </div>
          <div class="new-fields">
            <div class="field">
              <label for="new-portfolio-name">Name</label>
              <input
                id="new-portfolio-name"
                class="input"
                autocomplete="off"
                [attr.maxlength]="nameMax"
                [disabled]="!canManage()"
                [attr.aria-invalid]="nameError() ? true : null"
                [attr.aria-describedby]="nameError() ? 'new-portfolio-name-error' : null"
                [value]="newName()"
                (input)="newName.set($any($event.target).value)"
              />
              @if (nameError(); as e) {
                <span id="new-portfolio-name-error" class="error">{{ e }}</span>
              }
            </div>
            <div class="field">
              <label for="new-portfolio-cash">Starting cash (optional)</label>
              <input
                id="new-portfolio-cash"
                class="input num"
                type="number"
                inputmode="decimal"
                min="1"
                [attr.aria-invalid]="cashError() ? true : null"
                [attr.aria-describedby]="
                  cashError() ? 'new-portfolio-cash-error' : 'new-portfolio-cash-hint'
                "
                [disabled]="!canManage()"
                [value]="newCash()"
                (input)="newCash.set($any($event.target).value)"
              />
              @if (cashError(); as e) {
                <span id="new-portfolio-cash-error" class="error">{{ e }}</span>
              } @else {
                <span id="new-portfolio-cash-hint" class="hint"
                  >Empty uses the usual starting amount.</span
                >
              }
            </div>
            <div class="actions">
              <button
                type="submit"
                class="btn btn-primary"
                [disabled]="!canManage() || busy()"
                [attr.aria-busy]="busy()"
              >
                {{ busy() ? 'Opening…' : 'Open portfolio' }}
              </button>
              <app-permission-note permission="portfolio.manage" />
            </div>
          </div>
        </form>
      }
    </section>

    <app-sheet
      [open]="!!renaming()"
      labelledBy="rename-title"
      describedBy="rename-message"
      (dismiss)="closeRename()"
    >
      @if (renaming(); as p) {
        <form
          id="rename-portfolio-form"
          class="sheet-form"
          (submit)="$event.preventDefault(); rename()"
          novalidate
        >
          <h2 id="rename-title">Rename {{ p.name }}?</h2>
          <p id="rename-message" class="sheet-message">Only the name changes.</p>
          <div class="field">
            <label for="rename-portfolio">New name</label>
            <input
              id="rename-portfolio"
              class="input"
              autocomplete="off"
              [attr.maxlength]="nameMax"
              [value]="renameTo()"
              (input)="renameTo.set($any($event.target).value)"
            />
          </div>
          <div class="sheet-actions">
            <button type="button" class="btn" (click)="closeRename()">Cancel</button>
            <button type="submit" class="btn btn-primary" [disabled]="!canRename()">Rename</button>
          </div>
        </form>
      }
    </app-sheet>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
    }
    .books {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .books li {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      min-height: var(--row-h);
      padding: var(--space-2) 0;
      border-bottom: 1px solid var(--rule);
    }
    .name {
      display: inline-flex;
      align-items: baseline;
      gap: var(--space-2);
      min-width: 0;
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    .default {
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
    }
    .book-actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-1);
      margin-left: auto;
    }
    /* The new portfolio: what it is on the left, the fields on the right. */
    .new {
      display: grid;
      gap: var(--space-4) var(--space-8);
      padding: var(--space-5) 0 0;
    }
    @include bp.from-tablet {
      .new {
        grid-template-columns: minmax(0, 15rem) minmax(0, 1fr);
      }
    }
    .new-text h3 {
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
    }
    .new-text p {
      margin-top: 2px;
      font-size: var(--text-sm);
    }
    .new-fields {
      display: grid;
      gap: var(--space-4);
      align-items: start;
    }
    @media (min-width: 1024px) {
      .new-fields {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      grid-column: 1 / -1;
    }
  `,
})
export class PortfoliosPanel {
  private readonly api = inject(PortfoliosService);
  /** The one list of your portfolios (UX-69): the picker and this panel share it. */
  protected readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);

  protected readonly nameMax = NAME_MAX;
  protected readonly atBroker = atBroker;
  protected readonly books = this.ctx.options;
  protected readonly loadError = new Error(
    'Check your connection and try again. It also tries again on its own.',
  );
  protected readonly canManage = computed(() => this.session.can('portfolio.manage'));

  protected readonly newName = signal('');
  protected readonly newCash = signal('');
  protected readonly nameError = signal<string | null>(null);
  protected readonly cashError = signal<string | null>(null);
  protected readonly busy = signal(false);

  protected readonly renaming = signal<PortfolioRef | null>(null);
  protected readonly renameTo = signal('');
  protected readonly canRename = computed(() => {
    const name = this.renameTo().trim();
    return !this.busy() && name.length > 0 && name !== this.renaming()?.name;
  });

  protected async create(): Promise<void> {
    const name = this.newName().trim();
    const cashText = String(this.newCash()).trim();
    const cash = cashText ? Number(cashText) : null;
    this.nameError.set(name ? null : 'Enter a name.');
    this.cashError.set(
      cash === null || (Number.isFinite(cash) && cash > 0) ? null : 'Enter an amount above 0.',
    );
    if (this.nameError() || this.cashError()) return;
    this.busy.set(true);
    try {
      const made = await this.api.create({ name, initial_cash: cash });
      this.newName.set('');
      this.newCash.set('');
      await this.refresh();
      const start = made.initial_cash != null ? ` with ${formatMoney(made.initial_cash)}` : '';
      this.toasts.success(`Opened ${made.name}${start}. Follow a strategy on it to trade it.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }

  protected openRename(p: PortfolioRef): void {
    this.renameTo.set(p.name);
    this.renaming.set(p);
  }

  protected closeRename(): void {
    this.renaming.set(null);
  }

  protected async rename(): Promise<void> {
    const p = this.renaming();
    if (!p || !this.canRename()) return;
    this.busy.set(true);
    try {
      const done = await this.api.rename(p.id, this.renameTo().trim());
      this.closeRename();
      await this.refresh();
      this.toasts.success(`Renamed ${p.name} to ${done.name}.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }

  constructor() {
    void this.ctx.load();
  }

  protected reload(): void {
    void this.ctx.load(true);
  }

  private async refresh(): Promise<void> {
    await this.ctx.load(true);
  }
}
