import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { Draft } from '../../api/models';
import { StudioService } from '../../api/studio.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { StatusPill } from '../../shared/ui/status-pill';

type Stage = 'draft' | 'shadow' | 'active' | 'retired';

/**
 * Ship a draft: register it (it lands in shadow, trading on paper), then
 * enable it (active, trades from the next tick) or disable it (back to
 * shadow) with a toggle. Every step asks first.
 */
@Component({
  selector: 'app-draft-ship',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatusPill],
  templateUrl: './draft-ship.html',
  styleUrl: './draft-ship.scss',
})
export class DraftShip {
  private readonly studio = inject(StudioService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  readonly draft = input.required<Draft>();
  readonly ensureSaved = input<() => Promise<boolean>>(() => Promise.resolve(true));
  /** The draft as the API returned it after a register / enable / disable. */
  readonly changed = output<Draft>();

  protected readonly busy = signal(false);

  protected readonly stage = computed<Stage>(() => {
    const d = this.draft();
    if (d.status !== 'registered') return 'draft';
    return d.strategy_status ?? 'shadow';
  });
  protected readonly enabled = computed(() => this.stage() === 'active');
  protected readonly strategyId = computed(() => this.draft().registered_strategy_id ?? '');

  protected readonly steps: { id: Stage; label: string; detail: string }[] = [
    { id: 'draft', label: 'Draft', detail: 'Edit and test freely' },
    { id: 'shadow', label: 'Shadow', detail: 'Decides every tick, no orders' },
    { id: 'active', label: 'Active', detail: 'Places orders from the next tick' },
  ];

  protected stepState(id: Stage): 'done' | 'current' | 'todo' {
    const order: Stage[] = ['draft', 'shadow', 'active'];
    const current = order.indexOf(this.stage() === 'retired' ? 'shadow' : this.stage());
    const i = order.indexOf(id);
    return i < current ? 'done' : i === current ? 'current' : 'todo';
  }

  async register(): Promise<void> {
    const d = this.draft();
    const ok = await this.confirm.confirm({
      title: `Register ${d.name}?`,
      message:
        'The saved rules become a registered strategy in shadow: it is evaluated on every tick ' +
        'but places no orders until you enable it. Later edits to the draft do not change it.',
      confirmLabel: 'Register',
    });
    if (!ok) return;
    this.busy.set(true);
    try {
      if (!(await this.ensureSaved()())) return;
      const next = await this.studio.register(d.id);
      this.toasts.success(`Registered ${next.registered_strategy_id ?? d.name} in shadow.`);
      this.changed.emit(next);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }

  async toggle(): Promise<void> {
    return this.enabled() ? this.disable() : this.enable();
  }

  async enable(): Promise<void> {
    const id = this.strategyId();
    const ok = await this.confirm.confirm({
      title: `Enable ${id}?`,
      message:
        'It becomes active and places orders through the configured broker from the next tick.',
      confirmLabel: 'Enable',
      typedConfirmation: id,
    });
    if (!ok) return;
    await this.run(() => this.studio.enable(this.draft().id), `Enabled ${id}; it is now active.`);
  }

  async disable(): Promise<void> {
    const id = this.strategyId();
    const ok = await this.confirm.confirm({
      title: `Disable ${id}?`,
      message:
        'It goes back to shadow: it keeps being evaluated but places no new orders. Open ' +
        'positions are not closed.',
      confirmLabel: 'Disable',
      tone: 'danger',
    });
    if (!ok) return;
    await this.run(
      () => this.studio.disable(this.draft().id),
      `Disabled ${id}; it is back in shadow.`,
    );
  }

  private async run(call: () => Promise<Draft>, success: string): Promise<void> {
    this.busy.set(true);
    try {
      const next = await call();
      this.toasts.success(success);
      this.changed.emit(next);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }
}
