import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import { ConnectionsService } from '../../api/connections.service';
import { LiveService } from '../../api/live.service';
import { SubscriptionsService } from '../../api/subscriptions.service';
import { SessionService } from '../../core/auth/session.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { stageWords } from '../../shared/live-stages';
import { HelpTip } from '../../shared/ui/help-tip';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { STATE_WORDS, type StepState, atBroker, goingLiveSteps } from './going-live-steps';
import { lastPreview } from './preview-memory';

/**
 * Going live (F52): one checklist that walks the whole path from paper to
 * real money for one portfolio, in order: the server's broker gateway, the
 * broker connection, the portfolio's stage, the allocation, the account
 * profile, the safeguards, a dry-run preview, and the switch of a follow to
 * Approve each trade or Automatic. Each step shows whether it is done, who
 * acts (you or your admin) and a link to where it is done. It reads only;
 * every change happens on the linked page, with its own ticket and code.
 * Brass and the LIVE stamp show only once the portfolio is at a Real money
 * stage.
 *
 * `?portfolio=` picks the portfolio; else the one on screen.
 */
@Component({
  selector: 'app-going-live-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PageHeader, ModeStamp, HelpTip, EmptyState, ErrorState, LoadingState],
  template: `
    <app-page-header
      title="Going live"
      description="Every step from paper to real money for one portfolio, in order. Each says whether it is done and where to do it. Nothing changes here. Alerts only and Paper need none of these steps and no broker."
    >
      @if (stageLoaded()) {
        <app-mode-stamp [live]="realMoney()" />
      }
    </app-page-header>

    @if (ctx.state() === 'failed') {
      <app-error-state
        title="Could not load your portfolios"
        [error]="null"
        (retry)="ctx.load(true)"
      />
    } @else if (ctx.state() !== 'ready' && ctx.state() !== 'missing') {
      <app-loading-state label="Loading your portfolios" [rows]="4" />
    } @else if (!picked()) {
      <app-empty-state
        title="No portfolio yet"
        message="Open a paper portfolio and follow a strategy on it first. This checklist then shows the way to real money."
      >
        <a class="btn" routerLink="/welcome" [queryParams]="{ step: 'portfolio' }"
          >Open a portfolio</a
        >
      </app-empty-state>
    } @else {
      @let p = picked()!;
      <div class="top">
        @if (ctx.options().length > 1) {
          <div class="field picker">
            <label for="gl-portfolio">Portfolio</label>
            <select
              id="gl-portfolio"
              class="input"
              [value]="p.id"
              (change)="pick($any($event.target).value)"
            >
              @for (o of ctx.options(); track o.id) {
                <option [value]="o.id" [selected]="o.id === p.id">{{ o.name }}</option>
              }
            </select>
          </div>
        }
        <p class="progress" aria-live="polite">
          <strong class="num">{{ doneCount() }} of {{ steps().length }}</strong> steps done for
          <strong>{{ p.name }}</strong
          >.
          @if (stageLabel(); as s) {
            Stage now: {{ s }} <app-help-tip term="Portfolio stage" />
          }
        </p>
        <div
          class="bar"
          role="progressbar"
          aria-label="Steps done"
          aria-valuemin="0"
          [attr.aria-valuemax]="steps().length"
          [attr.aria-valuenow]="doneCount()"
        >
          <span [style.width.%]="(doneCount() / steps().length) * 100"></span>
        </div>
      </div>

      @if (readError(); as err) {
        <app-error-state title="Could not check every step" [error]="err" (retry)="retryReads()" />
      }

      <ol class="steps" [class.live]="realMoney()">
        @for (s of steps(); track s.key; let i = $index) {
          <li class="step" [attr.data-state]="s.state" [attr.data-step]="s.key">
            <span class="mark" aria-hidden="true">
              @if (s.state === 'done') {
                ✓
              } @else {
                {{ i + 1 }}
              }
            </span>
            <div class="body">
              <h2 class="title">
                <span class="visually-hidden">Step {{ i + 1 }}: </span>{{ s.title }}
              </h2>
              <p class="detail">{{ s.detail }}</p>
            </div>
            <span class="state" [attr.data-state]="s.state">{{ stateWords[s.state] }}</span>
            @if (s.link; as l) {
              <a
                class="btn"
                [class.btn-primary]="s.state === 'todo' && firstTodo() === s.key"
                [routerLink]="l.path"
                [fragment]="l.fragment"
                [queryParams]="l.query ?? null"
                >{{ l.label }}<span class="visually-hidden"> for {{ s.title }}</span></a
              >
            }
          </li>
        }
      </ol>

      <p class="foot muted">
        Real money moves only at the two Real money stages, and only for follows set to Approve each
        trade or Automatic. You can stop trading from any page at any time.
      </p>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .top {
      display: grid;
      gap: var(--space-2);
      max-width: 48rem;
    }
    .picker {
      max-width: 20rem;
    }
    .progress {
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .bar {
      height: 6px;
      border-radius: 3px;
      background: var(--color-surface-2);
      overflow: hidden;
    }
    .bar span {
      display: block;
      height: 100%;
      background: var(--color-gain);
      transition: width var(--dur-fast) var(--ease);
    }
    .steps {
      display: grid;
      gap: var(--space-2);
      max-width: 60rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .step {
      display: grid;
      grid-template-columns: 2rem minmax(0, 1fr) auto auto;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      background: var(--color-surface);

      @include bp.phone {
        grid-template-columns: 2rem minmax(0, 1fr);
        padding: var(--space-3);
      }
    }
    .step[data-state='done'] {
      background: var(--color-surface-2);
    }
    .mark {
      display: inline-grid;
      place-items: center;
      width: 2rem;
      height: 2rem;
      border: 1.5px solid var(--color-border-strong);
      border-radius: 50%;
      font-family: var(--font-mono);
      font-size: var(--text-sm);
      font-weight: var(--weight-bold);
    }
    .step[data-state='done'] .mark {
      border-color: var(--color-gain);
      background: var(--color-gain);
      color: var(--color-surface);
    }
    .title {
      font-size: var(--text-md);
      overflow-wrap: anywhere;
    }
    .detail {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .state {
      justify-self: start;
      padding: 0 var(--space-2);
      border: 1px solid var(--color-border-strong);
      border-radius: var(--radius-xs);
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
      white-space: nowrap;

      @include bp.phone {
        grid-column: 2;
      }
    }
    .state[data-state='done'] {
      border-color: var(--color-gain);
      color: var(--color-gain);
    }
    .state[data-state='admin'] {
      border-style: dashed;
    }
    .state[data-state='blocked'],
    .state[data-state='checking'] {
      color: var(--color-ink-3);
    }
    .step .btn {
      min-height: var(--touch-min);
      justify-content: center;
      white-space: nowrap;

      @include bp.phone {
        grid-column: 2;
        justify-self: stretch;
      }
    }
    /* Brass only where real money moves: the steps of a Real money portfolio. */
    .steps.live .step[data-state='done'] {
      border-left: var(--border-live) solid var(--color-live);
    }
    .foot {
      max-width: 60rem;
      font-size: var(--text-sm);
    }
  `,
})
export class GoingLivePage {
  protected readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly live = inject(LiveService);
  private readonly connectionsApi = inject(ConnectionsService);
  private readonly subscriptionsApi = inject(SubscriptionsService);
  private readonly router = inject(Router);

  /** `?portfolio=`: the portfolio to check. */
  readonly portfolio = input<string>();

  protected readonly stateWords = STATE_WORDS;

  private readonly chosen = linkedSignal(() => this.portfolio() ?? null);
  protected readonly picked = computed(() => {
    const id = this.chosen();
    const list = this.ctx.options();
    return (id ? list.find((p) => p.id === id) : undefined) ?? this.ctx.current();
  });
  private readonly brokerId = computed(() => {
    const p = this.picked();
    return p && atBroker(p) ? p.id : undefined;
  });

  private readonly gateways = resource({ loader: () => this.live.gateways() });
  private readonly providers = resource({ loader: () => this.connectionsApi.providers() });
  private readonly connections = resource({ loader: () => this.connectionsApi.list() });
  private readonly follows = resource({ loader: () => this.subscriptionsApi.list() });
  private readonly stage = resource({
    params: () => (this.brokerId() ? { id: this.brokerId()! } : undefined),
    loader: ({ params }) => this.live.stage(params.id),
  });
  private readonly report = resource({
    params: () => (this.brokerId() ? { id: this.brokerId()! } : undefined),
    loader: ({ params }) => this.live.gateReport(params.id),
  });
  private readonly allocation = resource({
    params: () => (this.brokerId() ? { id: this.brokerId()! } : undefined),
    loader: ({ params }) => this.live.allocation(params.id),
  });
  private readonly profile = resource({
    params: () => (this.brokerId() ? { id: this.brokerId()! } : undefined),
    loader: ({ params }) => this.live.profile(params.id),
  });
  private readonly rules = resource({
    params: () => (this.brokerId() ? { id: this.brokerId()! } : undefined),
    loader: ({ params }) => this.live.rules(params.id),
  });

  /** The portfolio's own reads: a failed one would leave its step on Checking for good. */
  private readonly bookReads = [this.stage, this.report, this.allocation, this.profile, this.rules];
  protected readonly readError = computed(
    () => this.bookReads.map((r) => r.error()).find((e) => e != null) ?? null,
  );

  protected retryReads(): void {
    for (const r of this.bookReads) if (r.error()) r.reload();
  }

  protected readonly steps = computed(() => {
    const p = this.picked();
    if (!p) return [];
    const value = <T>(r: { hasValue(): boolean; value(): T; error(): unknown }): T | undefined =>
      r.hasValue() ? r.value() : undefined;
    return goingLiveSteps({
      portfolio: p,
      isAdmin: this.session.me()?.role === 'admin',
      gateways: value(this.gateways) ?? (this.gateways.error() ? EMPTY_GATEWAYS : undefined),
      connections: value(this.connections) ?? (this.connections.error() ? [] : undefined),
      providers: value(this.providers) ?? (this.providers.error() ? [] : undefined),
      stage: value(this.stage),
      report: value(this.report),
      allocation: value(this.allocation),
      profile: this.profile.hasValue() ? this.profile.value() : undefined,
      rules: value(this.rules),
      follows: value(this.follows) ?? (this.follows.error() ? [] : undefined),
      previewedAt: lastPreview(p.id),
    });
  });

  protected readonly doneCount = computed(
    () => this.steps().filter((s) => s.state === 'done').length,
  );
  /** The first step still yours: its link is the primary button. */
  protected readonly firstTodo = computed(
    () => this.steps().find((s) => (s.state as StepState) === 'todo')?.key ?? null,
  );
  protected readonly stageLoaded = computed(() => !!this.brokerId() && this.stage.hasValue());
  /** Real money moves at this portfolio's stage: only then brass and LIVE. */
  protected readonly realMoney = computed(
    () => this.stageLoaded() && (this.stage.value()?.real_money ?? false),
  );
  protected readonly stageLabel = computed(() =>
    this.stageLoaded() ? stageWords(this.stage.value()?.stage ?? '').label : null,
  );

  constructor() {
    void this.ctx.load();
  }

  protected pick(id: string): void {
    this.chosen.set(id);
    void this.router.navigate([], { queryParams: { portfolio: id }, replaceUrl: true });
  }
}

const EMPTY_GATEWAYS = { configured: false, gateways: [] };
