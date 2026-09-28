import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  numberAttribute,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { JournalService } from '../../api/journal.service';
import type { JournalTradeDetailView, JournalTradeView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { formatMoney, toneClass } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { DateTimePipe, MoneyPipe, NumPipe } from '../../shared/format.pipes';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import {
  exitLabel,
  formatHolding,
  formatR,
  formatShare,
  parseLabels,
  sleeveLabel,
} from './journal-format';

type Plan = 'followed' | 'broke' | 'unsaid';

const PLANS: readonly SegmentOption<Plan>[] = [
  { value: 'followed', label: 'Followed' },
  { value: 'broke', label: 'Broke it' },
  { value: 'unsaid', label: 'Not said' },
];

const STOP_SOURCES: Record<string, string> = {
  order_plan: 'the trade plan on the order',
  protective_stop: 'the protective stop placed after entry',
};

/**
 * One trade: every leg with its excursions, R and exit efficiency, and the
 * review (tags, mistakes, playbook, whether you followed the plan, a note).
 * Saving the review needs `portfolio.manage`.
 */
@Component({
  selector: 'app-journal-trade-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    PermissionNote,
    Segmented,
    ErrorState,
    LoadingState,
    MoneyPipe,
    NumPipe,
    DateTimePipe,
  ],
  styleUrl: './journal.scss',
  template: `
    <app-page-header [title]="heading()" description="A trade from entry to exit, and your review.">
      <a actions class="btn btn-ghost" routerLink="/journal">Back to the journal</a>
    </app-page-header>

    @if (trade.error(); as err) {
      <app-error-state title="Could not load this trade" [error]="err" (retry)="trade.reload()" />
    } @else if (!trade.hasValue()) {
      <app-loading-state label="Loading the trade" [rows]="5" />
    } @else {
      @let t = trade.value();
      <section class="panel" aria-labelledby="legs-title">
        <div class="panel-head">
          <h2 id="legs-title">Entry and exits</h2>
          <span class="num" [class]="tone(t.pnl)">{{ money(t.pnl, t.legs[0].currency) }}</span>
        </div>
        <ul class="legs">
          @for (l of t.legs; track l.leg_id) {
            <li class="leg">
              <p class="leg-head">
                <strong>{{ l.quantity | num }} {{ t.ticker }}</strong>
                <span class="muted">{{ sleeve(t.sleeve, t.sleeve_name) }}, {{ t.side }}</span>
              </p>
              <dl class="facts">
                <div>
                  <dt>Entered</dt>
                  <dd>
                    {{ l.entry_at | dateTime }} at
                    {{ l.entry_price | money: { currency: l.currency } }}
                  </dd>
                </div>
                <div>
                  <dt>{{ l.is_open ? 'Last close' : 'Exited' }}</dt>
                  <dd>
                    @if (!l.is_open) {
                      {{ l.exit_at | dateTime }} at
                    }
                    {{ l.exit_price | money: { currency: l.currency } }}
                  </dd>
                </div>
                <div>
                  <dt>Held</dt>
                  <dd>{{ holding(l.holding_days) }}</dd>
                </div>
                <div>
                  <dt>P&amp;L</dt>
                  <dd class="num" [class]="tone(l.pnl)">{{ money(l.pnl, l.currency) }}</dd>
                </div>
                <div>
                  <dt>R multiple</dt>
                  <dd>{{ r(l.r_multiple) }}</dd>
                </div>
                <div>
                  <dt>Worst move</dt>
                  <dd>{{ share(l.mae_pct, true) }}</dd>
                </div>
                <div>
                  <dt>Best move</dt>
                  <dd>{{ share(l.mfe_pct, true) }}</dd>
                </div>
                <div>
                  <dt>Exit efficiency</dt>
                  <dd>{{ share(l.exit_efficiency) }}</dd>
                </div>
                <div>
                  <dt>Exit</dt>
                  <dd>{{ exit(l) }}</dd>
                </div>
              </dl>
              @if (l.stop_price !== null && l.stop_price !== undefined) {
                <p class="muted">
                  First stop {{ l.stop_price | money: { currency: l.currency } }}, from
                  {{ stopSource(l.stop_source) }}. At risk:
                  {{ l.risk_amount | money: { currency: l.currency } }}.
                </p>
              } @else {
                <p class="muted">No stop is known for this trade, so it has no R multiple.</p>
              }
            </li>
          }
        </ul>
      </section>

      <section class="panel" aria-labelledby="review-title">
        <div class="panel-head">
          <h2 id="review-title">Review</h2>
        </div>
        @if (canWrite()) {
          <form class="review" (submit)="$event.preventDefault(); save()">
            <div class="field">
              <label for="tags">Tags</label>
              <input
                id="tags"
                class="input"
                aria-describedby="tags-hint"
                [value]="tags()"
                (input)="tags.set($any($event.target).value)"
              />
              <p id="tags-hint" class="hint">
                Separate with commas.
                @if (labels.hasValue() && labels.value().tags.length) {
                  Used before: {{ labels.value().tags.join(', ') }}.
                }
              </p>
            </div>
            <div class="field">
              <label for="mistakes">Mistakes</label>
              <input
                id="mistakes"
                class="input"
                aria-describedby="mistakes-hint"
                [value]="mistakes()"
                (input)="mistakes.set($any($event.target).value)"
              />
              <p id="mistakes-hint" class="hint">
                Such as late entry or sized too big.
                @if (labels.hasValue() && labels.value().mistakes.length) {
                  Used before: {{ labels.value().mistakes.join(', ') }}.
                }
              </p>
            </div>
            <div class="field">
              <label for="playbook">Playbook</label>
              <select
                id="playbook"
                class="input"
                [value]="playbook()"
                (change)="playbook.set($any($event.target).value)"
              >
                <option value="">None</option>
                @if (playbooks.hasValue()) {
                  @for (p of playbooks.value(); track p.id) {
                    <option [value]="p.id" [selected]="p.id === playbook()">{{ p.name }}</option>
                  }
                }
              </select>
            </div>
            <div class="field">
              <span class="label">Did you follow your plan?</span>
              <app-segmented label="Did you follow your plan?" [options]="plans" [(value)]="plan" />
            </div>
            <div class="field">
              <label for="review-text">What happened</label>
              <textarea
                id="review-text"
                class="input"
                rows="3"
                maxlength="4000"
                [value]="text()"
                (input)="text.set($any($event.target).value)"
              ></textarea>
            </div>
            <button type="submit" class="btn btn-primary" [disabled]="saving()">Save review</button>
          </form>
        } @else {
          <dl class="facts">
            <div>
              <dt>Tags</dt>
              <dd>{{ t.tags.join(', ') || 'None' }}</dd>
            </div>
            <div>
              <dt>Mistakes</dt>
              <dd>{{ t.mistakes.join(', ') || 'None' }}</dd>
            </div>
            <div>
              <dt>Playbook</dt>
              <dd>{{ t.playbook_name ?? 'None' }}</dd>
            </div>
          </dl>
          <app-permission-note permission="portfolio.manage" />
        }
      </section>
    }
  `,
})
export class JournalTradePage {
  /** From the route `/journal/trades/:tradeId`. */
  readonly tradeId = input.required({ transform: numberAttribute });

  private readonly journal = inject(JournalService);
  private readonly toasts = inject(ToastService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);

  protected readonly plans = PLANS;
  protected readonly canWrite = computed(() => this.session.can('portfolio.manage'));
  protected readonly tags = signal('');
  protected readonly mistakes = signal('');
  protected readonly playbook = signal('');
  readonly plan = signal<Plan>('unsaid');
  protected readonly text = signal('');
  protected readonly saving = signal(false);

  protected readonly trade = resource({
    params: () => ({ id: this.tradeId(), portfolio: this.ctx.selectedId() }),
    loader: ({ params }) => this.journal.trade(params.id),
  });
  protected readonly playbooks = resource({ loader: () => this.journal.playbooks() });
  protected readonly labels = resource({
    params: () => ({ portfolio: this.ctx.selectedId() }),
    loader: () => this.journal.labels(),
  });

  protected readonly heading = computed(() => {
    if (!this.trade.hasValue()) return 'Trade';
    const t = this.trade.value();
    return `${t.side === 'short' ? 'Short' : 'Long'} ${t.ticker}`;
  });

  constructor() {
    // Fill the form from the stored review each time the trade loads.
    effect(() => {
      if (!this.trade.hasValue()) return;
      this.fill(this.trade.value());
    });
  }

  private fill(t: JournalTradeDetailView): void {
    this.tags.set(t.tags.join(', '));
    this.mistakes.set(t.mistakes.join(', '));
    this.playbook.set(t.playbook_id ?? '');
    this.plan.set(
      t.followed_plan === true ? 'followed' : t.followed_plan === false ? 'broke' : 'unsaid',
    );
    this.text.set(t.review ?? '');
  }

  protected async save(): Promise<void> {
    if (this.saving()) return;
    this.saving.set(true);
    const plan = this.plan();
    try {
      await this.journal.review(this.tradeId(), {
        tags: parseLabels(this.tags()),
        mistakes: parseLabels(this.mistakes()),
        playbook_id: this.playbook() || null,
        followed_plan: plan === 'unsaid' ? null : plan === 'followed',
        review: this.text().trim() || null,
      });
      this.toasts.success('Saved the review.');
      this.trade.reload();
      this.labels.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.saving.set(false);
    }
  }

  protected money(value: number, currency: string): string {
    return formatMoney(value, { currency, signed: true });
  }

  protected tone(value: number): string {
    return toneClass(value);
  }

  protected r(value: number | null | undefined): string {
    return formatR(value);
  }

  protected share(value: number | null | undefined, signed = false): string {
    return formatShare(value, signed);
  }

  protected holding(days: number): string {
    return formatHolding(days);
  }

  protected exit(l: JournalTradeView): string {
    return exitLabel(l.exit_trigger, l.is_open);
  }

  protected sleeve(s: string, name?: string | null): string {
    return sleeveLabel(s, name);
  }

  protected stopSource(source: string | null | undefined): string {
    return (source && STOP_SOURCES[source]) || 'a stop on record';
  }
}
