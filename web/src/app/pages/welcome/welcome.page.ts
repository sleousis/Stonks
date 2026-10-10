import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
  untracked,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import type {
  OnboardingStepView,
  OnboardingView,
  StepUpdate,
  StrategySummary,
} from '../../api/models';
import { type OnboardingStepId, OnboardingService } from '../../api/onboarding.service';
import { PortfoliosService } from '../../api/portfolios.service';
import { StrategiesService } from '../../api/strategies.service';
import { SubscriptionsService } from '../../api/subscriptions.service';
import { WatchlistsService } from '../../api/watchlists.service';
import { SessionService } from '../../core/auth/session.service';
import { ApiError } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { WatchlistContextService } from '../../core/watchlists/watchlist-context.service';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { NotificationSettings } from '../../shared/ui/notification-settings';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import {
  OPTIONAL_STEPS,
  STEP_COPY,
  SYSTEM_COPY,
  currentStep,
  orderedSteps,
  parseTickerText,
  progressText,
  setupComplete,
} from './welcome-steps';
import { STATUS_WORDS } from '../../shared/governance-labels';
import { strategyDisplayName } from '../../shared/strategy-names';
import { TelegramLink } from '../notifications/telegram-link';

/** The starting cash of a paper portfolio the guide opens. */
const DEFAULT_PAPER_CASH = 100000;

/**
 * The first-run guide: five plain steps, each can be skipped, progress kept
 * on the server for this person. The no-broker path comes first (follow a
 * strategy, turn on alerts), and the portfolio and watchlist steps are
 * optional (FR-01). Admins also see whether the install is
 * ready. Steps the data already shows done (a portfolio, a watchlist, a
 * strategy you follow, a device with alerts) tick themselves.
 */
@Component({
  selector: 'app-welcome-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    ModeStamp,
    NotificationSettings,
    TelegramLink,
    LoadingState,
    ErrorState,
    EmptyState,
  ],
  templateUrl: './welcome.page.html',
  styleUrl: './welcome.page.scss',
})
export class WelcomePage {
  /** Names, never ids (F18): a starter's title, else `Stocks on the move 3fa9`. */
  protected strategyName(st: StrategySummary): string {
    return strategyDisplayName(st.id, { starter: st.starter });
  }
  protected statusWord(status: StrategySummary['status']): string {
    return STATUS_WORDS[status] ?? status;
  }
  private readonly api = inject(OnboardingService);
  private readonly portfoliosApi = inject(PortfoliosService);
  private readonly watchlistsApi = inject(WatchlistsService);
  private readonly strategiesApi = inject(StrategiesService);
  private readonly subscriptionsApi = inject(SubscriptionsService);
  private readonly toasts = inject(ToastService);
  private readonly router = inject(Router);
  protected readonly session = inject(SessionService);
  protected readonly portfolios = inject(PortfolioContextService);
  private readonly watchlists = inject(WatchlistContextService);

  /** `?step=portfolio` opens that step first (links from empty money pages). */
  readonly step = input<string>();

  protected readonly copy = STEP_COPY;
  protected readonly systemCopy = SYSTEM_COPY;

  protected readonly guide = resource({ loader: () => this.api.get() });
  protected readonly system = resource({
    params: () => (this.session.isAdmin() ? { admin: true } : undefined),
    loader: () => this.api.system(),
  });
  protected readonly strategies = resource({
    loader: async (): Promise<StrategySummary[]> => {
      const [active, shadow] = await Promise.all([
        this.strategiesApi.list({ status: 'active', limit: 100 }),
        this.strategiesApi.list({ status: 'shadow', limit: 100 }),
      ]);
      return [...active.items, ...shadow.items];
    },
  });

  /** The step shown open: the one the trader picked, else the first to do. */
  protected readonly open = linkedSignal<OnboardingView | undefined, OnboardingStepId | null>({
    source: () => (this.guide.hasValue() ? this.guide.value() : undefined),
    computation: (view, previous) => {
      if (!view) return null;
      const asked = untracked(this.step);
      if (!previous?.source && asked && view.steps.some((s) => s.id === asked)) {
        return asked as OnboardingStepId;
      }
      const prev = previous?.value;
      const stillTodo = prev && view.steps.find((s) => s.id === prev)?.state === 'todo';
      return stillTodo ? prev : currentStep(view);
    },
  });
  /** The steps in the guide's order: follow and alerts before the optional ones. */
  protected readonly steps = computed(() =>
    this.guide.hasValue() ? orderedSteps(this.guide.value().steps) : [],
  );
  /** Done once every step that matters is handled; the optional ones may wait. */
  protected readonly complete = computed(
    () => this.guide.hasValue() && setupComplete(this.guide.value().steps),
  );
  protected readonly progress = computed(() =>
    this.guide.hasValue() ? progressText(this.guide.value().steps) : '',
  );
  protected readonly busy = signal<string | null>(null);

  // ---- form state -------------------------------------------------------------
  protected readonly portfolioName = signal('Paper portfolio');
  protected readonly portfolioCash = signal(String(DEFAULT_PAPER_CASH));
  protected readonly listName = signal('My watchlist');
  protected readonly listTickers = signal('');
  protected readonly followStrategy = signal('');
  protected readonly followMode = signal<'notify' | 'paper'>('notify');
  protected readonly followPortfolio = signal('');
  /** The portfolio a Paper follow goes to: the one picked here, else the current one. */
  protected readonly followBook = computed(
    () => this.followPortfolio() || this.portfolios.current()?.id || '',
  );
  protected readonly formError = signal<string | null>(null);

  constructor() {
    void this.portfolios.load();
  }

  protected tagWord(step: OnboardingStepView): string {
    if (step.state === 'done') return 'Done';
    if (step.state === 'skipped') return 'Skipped';
    return OPTIONAL_STEPS.has(step.id) ? 'Optional' : 'To do';
  }

  protected toggle(step: OnboardingStepId): void {
    this.formError.set(null);
    this.open.update((current) => (current === step ? null : step));
  }

  protected async mark(step: OnboardingStepId, state: StepUpdate['state']): Promise<void> {
    await this.run(`mark-${step}`, async () => {
      this.guide.set(await this.api.setStep(step, state));
    });
  }

  protected async createPortfolio(event: Event): Promise<void> {
    event.preventDefault();
    const name = this.portfolioName().trim();
    const cash = Number(this.portfolioCash());
    if (!name) return this.formError.set('Give the portfolio a name.');
    if (!Number.isFinite(cash) || cash <= 0) {
      return this.formError.set('Starting cash must be a positive amount.');
    }
    await this.run('portfolio', async () => {
      const made = await this.portfoliosApi.create({ name, initial_cash: cash });
      await this.portfolios.load(true);
      this.portfolios.select(made.id);
      this.toasts.success(`Opened ${made.name}.`);
      this.guide.reload();
    });
  }

  protected async createWatchlist(event: Event): Promise<void> {
    event.preventDefault();
    const name = this.listName().trim();
    const tickers = parseTickerText(this.listTickers());
    if (!name) return this.formError.set('Give the watchlist a name.');
    if (!tickers.length) return this.formError.set('Add at least one ticker, like AAPL.US.');
    await this.run('data', async () => {
      const made = await this.watchlistsApi.create({ name, tickers });
      await this.watchlists.load(true);
      this.toasts.success(`Saved ${made.name} with ${made.tickers.length} tickers.`);
      this.guide.reload();
    });
  }

  protected async follow(event: Event): Promise<void> {
    event.preventDefault();
    const strategy = this.followStrategy();
    const mode = this.followMode();
    if (!strategy) return this.formError.set('Pick a strategy to follow.');
    const st = this.strategies.value()?.find((x) => x.id === strategy);
    const name = st ? this.strategyName(st) : strategyDisplayName(strategy);
    await this.run('follow', async () => {
      let portfolio = this.followBook();
      // Paper with no portfolio yet: open one with the default cash (FR-01).
      if (mode === 'paper' && !portfolio) {
        const made = await this.portfoliosApi.create({
          name: 'Paper portfolio',
          initial_cash: DEFAULT_PAPER_CASH,
        });
        await this.portfolios.load(true);
        this.portfolios.select(made.id);
        portfolio = made.id;
      }
      await this.subscriptionsApi.subscribe({
        strategy_id: strategy,
        mode,
        portfolio_id: mode === 'paper' ? portfolio : null,
      });
      this.toasts.success(
        mode === 'paper' ? `Paper trading ${name}.` : `Following ${name} for signals.`,
      );
      this.guide.reload();
    });
  }

  protected async close(): Promise<void> {
    await this.run('close', async () => {
      await this.api.setDismissed(true);
      await this.router.navigateByUrl('/');
    });
  }

  protected async reopen(): Promise<void> {
    await this.run('reopen', async () => {
      this.guide.set(await this.api.setDismissed(false));
    });
  }

  private async run(key: string, work: () => Promise<void>): Promise<void> {
    this.busy.set(key);
    this.formError.set(null);
    try {
      await work();
    } catch (err) {
      // Mutations are toasted by the error interceptor; keep the reason by the form too.
      if (err instanceof ApiError) this.formError.set(err.message);
    } finally {
      this.busy.set(null);
    }
  }
}
