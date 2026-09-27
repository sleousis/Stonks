import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { SubscriptionsService } from '../../api/subscriptions.service';
import { SessionService } from '../../core/auth/session.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { MODES, type ModeOption, modeLabel } from '../../shared/governance-labels';
import { strategyDisplayName } from '../../shared/strategy-names';
import { HelpTip } from '../../shared/ui/help-tip';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PermissionNote } from '../../shared/ui/permission-note';
import { ErrorState, LoadingState } from '../../shared/ui/states';

/** How a new follower starts. Auto is never a starting mode. */
export type FollowMode = 'notify' | 'paper';

/** The starting modes, in the same words as Today's switch (UX-31). */
export const FOLLOW_MODES = MODES.filter(
  (m): m is ModeOption & { value: FollowMode } => m.value !== 'auto',
);

/**
 * Strategy page: follow this strategy, for signals only or paper trading in
 * one of your portfolios. Once followed, it says so and points to Today,
 * where the mode is changed (auto, after the paper record, asks for a code).
 */
@Component({
  selector: 'app-follow-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, HelpTip, ModeStamp, PermissionNote, ErrorState, LoadingState],
  template: `
    <section class="panel" aria-labelledby="follow-title">
      <div class="panel-head">
        <h2 id="follow-title">Follow</h2>
      </div>
      @if (subs.error(); as err) {
        <app-error-state
          title="Could not load what you follow"
          [error]="err"
          (retry)="subs.reload()"
        />
      } @else if (!subs.hasValue()) {
        <app-loading-state label="Loading what you follow" [rows]="2" />
      } @else if (mine(); as s) {
        <div class="panel-body following">
          <p>
            You follow this strategy:
            <strong>{{ modeLabel(s.mode) }}</strong>
            @if (s.portfolio_id) {
              on {{ portfolioName(s.portfolio_id) }}
            }
            @if (!s.enabled) {
              <span class="muted">(switched off)</span>
            }
          </p>
          <a routerLink="/" class="btn">Change it on Today</a>
        </div>
      } @else {
        <form
          class="panel-body follow"
          (submit)="$event.preventDefault(); follow()"
          novalidate
          aria-describedby="follow-lead"
        >
          <p id="follow-lead" class="lead">
            Get its signals, or let it paper trade. Real money comes later, from Today, after enough
            paper days.
          </p>
          <fieldset class="modes">
            <legend class="visually-hidden">How to follow</legend>
            @for (m of modes; track m.value) {
              <label class="mode" [class.picked]="mode() === m.value">
                <input
                  type="radio"
                  name="follow-mode"
                  [value]="m.value"
                  [checked]="mode() === m.value"
                  [disabled]="!canTrade()"
                  (change)="mode.set(m.value)"
                />
                <span class="mode-text">
                  <strong>{{ m.label }} <app-help-tip [term]="m.label" /></strong>
                  <span class="muted">{{ m.help }}</span>
                </span>
              </label>
            }
          </fieldset>
          @if (mode() === 'paper') {
            @if (portfolios().length === 0) {
              <p class="note">
                You have no portfolio yet. Open a paper portfolio on your
                <a routerLink="/profile">profile</a> first.
              </p>
            } @else {
              <div class="field">
                <label for="follow-portfolio">Portfolio</label>
                <div class="pick">
                  <select
                    id="follow-portfolio"
                    class="input"
                    [disabled]="!canTrade()"
                    [value]="portfolioId()"
                    (change)="portfolioId.set($any($event.target).value)"
                  >
                    @for (p of portfolios(); track p.id) {
                      <option [value]="p.id" [selected]="p.id === portfolioId()">
                        {{ p.name }}
                      </option>
                    }
                  </select>
                  <app-mode-stamp [live]="false" />
                </div>
                <span class="hint">Paper trades never reach a real broker account.</span>
              </div>
            }
          }
          <div class="actions">
            <button
              type="submit"
              class="btn btn-primary"
              [disabled]="!canFollow()"
              [attr.aria-busy]="busy()"
            >
              {{ busy() ? 'Following…' : 'Follow' }}
            </button>
            @if (!canTrade()) {
              <app-permission-note permission="portfolio.trade" />
            }
          </div>
        </form>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
    }
    .following,
    .follow {
      display: grid;
      gap: var(--space-3);
      justify-items: start;
    }
    .lead {
      color: var(--color-ink-2);
    }
    .modes {
      display: grid;
      gap: var(--space-2);
      width: 100%;
      margin: 0;
      padding: 0;
      border: 0;
    }
    .mode {
      display: flex;
      align-items: flex-start;
      gap: var(--space-2);
      min-height: var(--touch-min);
      padding: var(--space-2) var(--space-3);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      cursor: pointer;
    }
    .mode.picked {
      border-color: var(--color-primary);
    }
    .mode input {
      margin-top: 3px;
    }
    .mode-text {
      display: grid;
      gap: 2px;
    }
    .field {
      width: 100%;
    }
    .pick {
      display: flex;
      align-items: center;
      gap: var(--space-2);
    }
    .pick select {
      min-width: 0;
      flex: 1;
    }
    .note {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-info);
      border-radius: var(--radius-sm);
      background: var(--color-info-soft);
      font-size: var(--text-sm);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
  `,
})
export class FollowPanel {
  readonly strategyId = input.required<string>();

  private readonly api = inject(SubscriptionsService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);

  protected readonly modes = FOLLOW_MODES;
  protected readonly subs = resource({ loader: () => this.api.list() });
  protected readonly canTrade = computed(() => this.session.can('portfolio.trade'));
  protected readonly mine = computed(() =>
    this.subs.hasValue()
      ? (this.subs.value().find((s) => s.strategy_id === this.strategyId()) ?? null)
      : null,
  );
  protected readonly portfolios = computed(() =>
    this.ctx.options().filter((p) => p.status !== 'archived'),
  );
  protected readonly mode = signal<FollowMode>('notify');
  protected readonly portfolioId = linkedSignal(
    () => this.ctx.current()?.id ?? this.portfolios()[0]?.id ?? '',
  );
  protected readonly busy = signal(false);
  protected readonly canFollow = computed(
    () =>
      this.canTrade() &&
      !this.busy() &&
      (this.mode() === 'notify' || (!!this.portfolioId() && this.portfolios().length > 0)),
  );

  constructor() {
    void this.ctx.load();
  }

  protected readonly modeLabel = modeLabel;

  protected portfolioName(id: string): string {
    return this.ctx.options().find((p) => p.id === id)?.name ?? 'one of your portfolios';
  }

  protected async follow(): Promise<void> {
    if (!this.canFollow()) return;
    const paper = this.mode() === 'paper';
    const name = strategyDisplayName(this.strategyId());
    this.busy.set(true);
    try {
      await this.api.subscribe({
        strategy_id: this.strategyId(),
        mode: this.mode(),
        portfolio_id: paper ? this.portfolioId() : null,
      });
      this.subs.reload();
      this.toasts.success(
        paper
          ? `Following ${name} on paper in ${this.portfolioName(this.portfolioId())}.`
          : `Following ${name} for signals.`,
      );
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }
}
