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
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { promoteThroughGate } from '../../shared/governance';
import {
  LIFECYCLE,
  STAGES,
  type Stage as LifeStage,
  stageState,
} from '../../shared/governance-labels';
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
  imports: [RouterLink, StatusPill, StatusChangeDialog, PermissionNote],
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

  /** Draft, Paper and Live (Ready is shown on the strategy page, after the go-live check). */
  protected readonly steps = STAGES.filter((s) => s.id !== 'ready');

  protected stepState(id: LifeStage): 'done' | 'current' | 'todo' {
    const stage = this.stage();
    const current: LifeStage = stage === 'draft' ? 'draft' : stage === 'active' ? 'live' : 'paper';
    return stageState(id, current);
  }

  async register(): Promise<void> {
    if (!this.canShip()) return;
    const d = this.draft();
    const ok = await this.confirm.confirm({
      title: `Start paper trading ${d.name}?`,
      message:
        'The saved rules become a registered strategy that trades on paper. It decides on every ' +
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
    const next = await promoteThroughGate({
      id,
      dialog: this.dialog(),
      toasts: this.toasts,
      golive: () => this.strategies.golive(id),
      promote: (body) => this.studio.enable(draftId, body, true),
      title: `Go live with ${id}?`,
      message: 'It places orders through the broker from the next trading run.',
      confirmLabel: LIFECYCLE.live.label,
      busy: (on) => this.busy.set(on),
    });
    if (!next) return;
    this.toasts.success(LIFECYCLE.live.done(id));
    this.changed.emit(next);
  }

  async disable(): Promise<void> {
    if (!this.canShip()) return;
    const id = this.strategyId();
    const body = await this.dialog().open({
      title: `Move ${id} back to paper trading?`,
      message:
        'It keeps deciding on every run but places no new orders. Open positions are not closed.',
      confirmLabel: LIFECYCLE.pause.label,
      tone: 'danger',
      minReason: 1,
    });
    if (!body) return;
    this.busy.set(true);
    try {
      const next = await this.studio.disable(this.draft().id, body);
      this.toasts.success(LIFECYCLE.pause.done(id));
      this.changed.emit(next);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }
}
