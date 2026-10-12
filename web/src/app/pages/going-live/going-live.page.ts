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
import { type LiveStage, STAGES, STAGE_WORDS, stageWords } from '../../shared/live-stages';
import { HelpTip } from '../../shared/ui/help-tip';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PageHeader } from '../../shared/ui/page-header';
import { StatusPill } from '../../shared/ui/status-pill';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { STATE_WORDS, type StepState, atBroker, goingLiveSteps } from './going-live-steps';
import { lastPreview } from './preview-memory';

/**
 * Going live (F52): one checklist that walks the whole path from paper to
 * real money for one portfolio, in order: the broker connection, the
 * server's link to Interactive Brokers (IBKR only), the portfolio's stage, the allocation, the account
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
  imports: [
    RouterLink,
    PageHeader,
    ModeStamp,
    HelpTip,
    StatusPill,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header title="Going live" [description]="standfirst()">
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
        <a class="btn btn-primary" routerLink="/welcome" [queryParams]="{ step: 'portfolio' }"
          >Open a portfolio</a
        >
      </app-empty-state>
    } @else {
      @let p = picked()!;
      <!-- The stage path: four stages on one track. The current one in accent,
           the real-money stages in brass. -->
      <section class="path" aria-labelledby="path-title">
        <h2 id="path-title" class="visually-hidden">Stage path</h2>
        <ol class="stages">
          @for (st of stagePath(); track st.id) {
            <li
              [attr.data-state]="st.state"
              [class.live]="st.live"
              [attr.aria-current]="st.state === 'current' ? 'step' : null"
            >
              <span class="node" aria-hidden="true"></span>
              <span class="stage-label">{{ st.label }}</span>
              <span class="stage-means">{{ st.means }}</span>
            </li>
          }
        </ol>
      </section>

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
          <strong class="num">{{ doneCount() }} of {{ total() }}</strong> steps done for
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
          [attr.aria-valuemax]="total()"
          [attr.aria-valuenow]="doneCount()"
        >
          <span [style.width.%]="(doneCount() / total()) * 100"></span>
        </div>
      </div>

      @if (readError(); as err) {
        <app-error-state title="Could not check every step" [error]="err" (retry)="retryReads()" />
      }

      <ol class="steps" [class.live]="realMoney()">
        @for (s of steps(); track s.key; let i = $index) {
          @if (s.key === 'later') {
            <!-- The steps that wait for a broker, as one muted line (PO-12). -->
            <li class="later muted" data-step="later">{{ s.title }}</li>
          } @else {
            <li class="step" [attr.data-state]="s.state" [attr.data-step]="s.key">
              <span class="mark" aria-hidden="true">
                @if (s.state === 'done') {
                  ✓
                } @else {
                  {{ numbers()[i] }}
                }
              </span>
              <div class="body">
                <h2 class="title">
                  <span class="visually-hidden">Step {{ numbers()[i] }}: </span>{{ s.title }}
                </h2>
                <p class="detail">{{ s.detail }}</p>
              </div>
              <app-status-pill
                class="state"
                [attr.data-state]="s.state"
                [status]="pillKeys[s.state]"
                [label]="stateWords[s.state]"
              />
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
        }
      </ol>

      <div class="foot callout">
        <p>
          Real money moves only at the two Real money stages, and only for follows set to Approve
          each trade or Automatic. You can stop trading from any page at any time.
        </p>
        <p>
          Nothing changes on this page. Alerts only and Paper need none of these steps and no
          broker.
        </p>
      </div>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: grid;
      gap: var(--space-6);
      min-width: 0;
    }

    /* Stage path: a horizontal stepper on one 2px track. */
    .path {
      padding-bottom: var(--space-5);
      border-bottom: 1px solid var(--rule);
    }
    .stages {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .stages li {
      position: relative;
      display: grid;
      align-content: start;
      gap: var(--space-1);
      min-width: 0;
      padding-right: var(--space-3);
    }
    .stages li::before {
      content: '';
      position: absolute;
      top: 7px;
      left: 16px;
      right: 0;
      height: 2px;
      background: var(--rule);
    }
    .stages li:last-child::before {
      display: none;
    }
    .stages li[data-state='done']::before {
      background: var(--ink);
    }
    .node {
      position: relative;
      width: 16px;
      height: 16px;
      margin-bottom: var(--space-2);
      border: 2px solid var(--rule-control);
      border-radius: 50%;
      background: var(--bg);
    }
    li[data-state='done'] .node {
      border-color: var(--ink);
      background: var(--ink);
    }
    li[data-state='current'] .node {
      border-color: var(--accent);
      background: var(--accent);
      box-shadow: 0 0 0 4px var(--accent-soft);
    }
    /* Real-money stages: brass ring, a brass fill once reached. */
    li.live .node {
      border-color: var(--brass-fill);
    }
    li.live[data-state='done'] .node,
    li.live[data-state='current'] .node {
      border-color: var(--brass-fill);
      background: var(--brass-fill);
      box-shadow: 0 0 0 4px var(--brass-soft);
    }
    .stage-label {
      font-size: var(--text-md);
      font-weight: var(--weight-semibold);
    }
    li[data-state='todo'] .stage-label {
      color: var(--muted);
      font-weight: var(--weight-label);
    }
    li[data-state='current'] .stage-label {
      color: var(--accent);
    }
    li.live .stage-label {
      color: var(--brass);
    }
    .stage-means {
      max-width: 28ch;
      color: var(--muted);
      font-size: var(--text-xs);
      line-height: 1.4;
    }
    @include bp.phone {
      .stage-means {
        display: none;
      }
      .stage-label {
        font-size: var(--text-sm);
      }
    }

    .top {
      display: grid;
      gap: var(--space-3);
      max-width: 48rem;
    }
    .picker {
      max-width: 20rem;
    }
    .progress {
      color: var(--ink-2);
      font-size: var(--text-lead);
      overflow-wrap: anywhere;
    }
    .bar {
      height: 4px;
      border-radius: 2px;
      background: var(--rule);
      overflow: hidden;
    }
    .bar span {
      display: block;
      height: 100%;
      background: var(--accent);
      transition: width var(--dur-fast) var(--ease-out);
    }
    /* The gates: rows on a 2px ink rule, a hairline between. No box. */
    .steps {
      display: grid;
      max-width: 64rem;
      margin: 0;
      padding: 0;
      list-style: none;
      border-top: 2px solid var(--rule-strong);
    }
    .steps > li {
      border-bottom: 1px solid var(--rule);
    }
    .step {
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr) auto auto;
      align-items: center;
      gap: var(--space-2) var(--space-4);
      padding: var(--space-4) 0;

      @include bp.phone {
        grid-template-columns: 28px minmax(0, 1fr);
        gap: var(--space-2) var(--space-3);
      }
    }
    .later {
      padding: var(--space-3) 0 var(--space-3) calc(28px + var(--space-4));
      font-size: var(--text-sm);
    }
    /* The step number in a small sunk squircle; a filled check once done. */
    .mark {
      display: inline-grid;
      place-items: center;
      width: 28px;
      height: 28px;
      border-radius: var(--r-ctl);
      background: var(--sunk);
      color: var(--ink-2);
      font-family: var(--font-mono);
      font-size: var(--text-xs);
      font-weight: var(--weight-semibold);
      font-variant-numeric: tabular-nums slashed-zero;
    }
    .step[data-state='done'] .mark {
      background: var(--up-soft);
      color: var(--up);
    }
    .body {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .title {
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
      letter-spacing: normal;
      overflow-wrap: anywhere;
    }
    .detail {
      max-width: 68ch;
      color: var(--ink-2);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .state {
      justify-self: start;

      @include bp.phone {
        grid-column: 2;
      }
    }
    .step .btn {
      justify-content: center;
      white-space: nowrap;

      @include bp.phone {
        grid-column: 2;
        justify-self: stretch;
      }
    }
    /* Brass only where real money moves: the done steps of a Real money portfolio. */
    .steps.live .step[data-state='done'] .mark {
      background: var(--brass-soft);
      color: var(--brass);
    }
    .foot {
      display: grid;
      gap: var(--space-1);
      max-width: 64rem;
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
  /** Each step state as a status pill: Done a positive receipt, the rest neutral. */
  protected readonly pillKeys: Record<StepState, string> = {
    done: 'done',
    todo: 'not_yet',
    admin: 'waiting_admin',
    blocked: 'waiting',
    checking: 'running',
  };

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

  /** Every step, counting the ones the muted line stands for. */
  protected readonly total = computed(() =>
    this.steps().reduce((sum, s) => sum + (s.span ?? 1), 0),
  );
  /** Each entry's step number, after the steps a muted line stands for. */
  protected readonly numbers = computed(() => {
    let next = 1;
    return this.steps().map((s) => {
      const n = next;
      next += s.span ?? 1;
      return n;
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
  /** The four stages with where this portfolio stands: a paper portfolio sits at Simulated. */
  protected readonly stagePath = computed(() => {
    const now: LiveStage = this.stageLoaded()
      ? ((this.stage.value()?.stage as LiveStage | undefined) ?? 'sim_paper')
      : 'sim_paper';
    const at = STAGES.indexOf(now);
    return STAGES.map((id, i) => ({
      id,
      label: STAGE_WORDS[id].label,
      means: STAGE_WORDS[id].means,
      live: STAGE_WORDS[id].live,
      state: i < at ? 'done' : i === at ? 'current' : 'todo',
    }));
  });
  /** The standfirst, from the checklist: "3 of 8 steps done for Main. Stage now: Simulated." */
  protected readonly standfirst = computed(() => {
    const p = this.picked();
    if (!p || !this.steps().length) {
      return 'Every step from paper to real money for one portfolio, in order.';
    }
    const stage = this.stageLabel();
    const head = `${this.doneCount()} of ${this.total()} steps done for ${p.name}.`;
    return stage ? `${head} Stage now: ${stage}.` : head;
  });
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
