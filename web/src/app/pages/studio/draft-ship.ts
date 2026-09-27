import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { Draft } from '../../api/models';
import { StrategiesService } from '../../api/strategies.service';
import { StudioService } from '../../api/studio.service';
import { SubscriptionsService } from '../../api/subscriptions.service';
import { SystemService } from '../../api/system.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { demoteOptions, promoteThroughGate } from '../../shared/governance';
import { LIFECYCLE } from '../../shared/governance-labels';
import { StageBar } from '../strategies/stage-bar';
import { PermissionNote } from '../../shared/ui/permission-note';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { StatusPill } from '../../shared/ui/status-pill';

type Stage = 'draft' | 'shadow' | 'active' | 'retired';

/**
 * Ship a draft: start paper trading it (it is registered in shadow), then
 * go live (active, trades from the next run) or move it back to paper
 * trading. Every step asks first. Same words as the strategy page
 * (`shared/governance-labels.ts`).
 */
@Component({
  selector: 'app-draft-ship',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatusPill, StatusChangeDialog, PermissionNote, StageBar],
  templateUrl: './draft-ship.html',
  styleUrl: './draft-ship.scss',
})
export class DraftShip {
  private readonly studio = inject(StudioService);
  private readonly strategies = inject(StrategiesService);
  private readonly dialog = viewChild.required(StatusChangeDialog);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly session = inject(SessionService);
  private readonly system = inject(SystemService);
  private readonly subscriptions = inject(SubscriptionsService);
  private readonly portfolioCtx = inject(PortfolioContextService);

  readonly draft = input.required<Draft>();
  readonly ensureSaved = input<() => Promise<boolean>>(() => Promise.resolve(true));
  /** The draft as the API returned it after a register / enable / disable. */
  readonly changed = output<Draft>();

  protected readonly busy = signal(false);
  protected readonly labels = LIFECYCLE;
  /** Starting paper trading, going live and back need `strategy.promote`. */
  protected readonly canShip = computed(() => this.session.can('strategy.promote'));

  protected readonly stage = computed<Stage>(() => {
    const d = this.draft();
    if (d.status !== 'registered') return 'draft';
    return d.strategy_status ?? 'shadow';
  });
  protected readonly enabled = computed(() => this.stage() === 'active');
  protected readonly strategyId = computed(() => this.draft().registered_strategy_id ?? '');

  /** The stage bar's status: the draft, or the strategy's own status. */
  protected readonly barStatus = computed(() => {
    const stage = this.stage();
    return stage === 'draft' ? ('draft' as const) : stage;
  });

  async register(): Promise<void> {
    if (!this.canShip()) return;
    const d = this.draft();
    const ok = await this.confirm.confirm({
      title: `Start paper trading ${d.name}?`,
      message:
        'The saved rules become a strategy that trades on paper. It decides on every ' +
        'run but places no real orders until you go live. Later edits to the draft do not change it.',
      confirmLabel: LIFECYCLE.paper.label,
    });
    if (!ok) return;
    this.busy.set(true);
    try {
      if (!(await this.ensureSaved()())) return;
      const next = await this.studio.register(d.id);
      this.toasts.success(LIFECYCLE.paper.done(next.registered_strategy_id ?? d.name));
      this.changed.emit(next);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }

  /** Same go-live gate as promoting in Strategies: report first, override on a 409. */
  async enable(): Promise<void> {
    if (!this.canShip()) return;
    const id = this.strategyId();
    const draftId = this.draft().id;
    const name = this.draft().name;
    const next = await promoteThroughGate({
      id,
      name,
      dialog: this.dialog(),
      toasts: this.toasts,
      golive: () => this.strategies.golive(id),
      promote: (body) => this.studio.enable(draftId, body, true),
      broker: () => this.system.broker(),
      followers: () => this.followers(id),
      title: `Go live with ${name}?`,
      message: 'It places orders through the broker from the next trading run.',
      confirmLabel: LIFECYCLE.live.label,
      busy: (on) => this.busy.set(on),
    });
    if (!next) return;
    this.toasts.success(LIFECYCLE.live.done(name));
    this.changed.emit(next);
  }

  async disable(): Promise<void> {
    if (!this.canShip()) return;
    const name = this.draft().name;
    const body = await this.dialog().open(demoteOptions('pause', name));
    if (!body) return;
    this.busy.set(true);
    try {
      const next = await this.studio.disable(this.draft().id, body);
      this.toasts.success(LIFECYCLE.pause.done(name));
      this.changed.emit(next);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }

  /** Names of the portfolios that trade it on paper or auto, for the go-live ticket. */
  private async followers(id: string): Promise<string[]> {
    const subs = await this.subscriptions.list();
    const names = this.portfolioCtx.options();
    return subs
      .filter((s) => s.strategy_id === id && s.portfolio_id && s.mode !== 'notify')
      .map((s) => names.find((p) => p.id === s.portfolio_id)?.name ?? 'One of your portfolios');
  }
}
