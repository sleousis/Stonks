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

import { LiveService } from '../../api/live.service';
import type { OptionsLiveView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDateTime } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PermissionNote } from '../../shared/ui/permission-note';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

const REASON_MAX = 500;

export type OptionsLevel = OptionsLiveView['level'];

export const LEVEL_OPTIONS: SegmentOption<OptionsLevel>[] = [
  { value: 'none', label: 'None' },
  { value: 'covered', label: 'Covered' },
  { value: 'spreads', label: 'Spreads' },
  { value: 'naked', label: 'Naked' },
];

/** "Options live: off" or "Options live: on", the card's headline. */
export function optionsHeadline(view: Pick<OptionsLiveView, 'allowed'>): string {
  return view.allowed ? 'Options live: on' : 'Options live: off';
}

/** The label of a level. */
export function levelLabel(level: string): string {
  return LEVEL_OPTIONS.find((o) => o.value === level)?.label ?? level;
}

/**
 * Live options of one real-money portfolio (roadmap 17.8). Off by default.
 * The card says "Options live: off" with every reason (the admin's switch,
 * the stage, the approval level), what each approval level allows, and
 * lets the owner set the level with a reason and a fresh code. Setting a
 * level never turns options on by itself. Every option order waits for
 * approval on the Tickets page.
 */
@Component({
  selector: 'app-live-options-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PermissionNote, Segmented, StatusPill, ErrorState, LoadingState],
  templateUrl: './live-options-card.html',
  styleUrl: './live-options-card.scss',
})
export class LiveOptionsCard {
  private readonly live = inject(LiveService);
  private readonly confirm = inject(ConfirmService);
  private readonly stepUp = inject(StepUpService);
  private readonly toasts = inject(ToastService);
  private readonly session = inject(SessionService);

  readonly portfolioId = input.required<string>();
  readonly portfolioName = input.required<string>();

  protected readonly canManage = computed(() => this.session.can('live.manage'));
  protected readonly levels = LEVEL_OPTIONS;
  protected readonly reasonMax = REASON_MAX;
  protected readonly headline = optionsHeadline;
  protected readonly label = levelLabel;
  protected readonly dateTime = formatDateTime;

  protected readonly view = resource({
    params: () => ({ id: this.portfolioId() }),
    loader: ({ params }) => this.live.optionsLive(params.id),
  });

  protected readonly level = linkedSignal<OptionsLevel>(() =>
    this.view.hasValue() ? this.view.value().level : 'none',
  );
  protected readonly reason = signal('');
  protected readonly tried = signal(false);
  protected readonly saving = signal(false);

  protected readonly reasonError = computed(() => {
    const r = this.reason();
    if (!r.trim()) return 'Say why. It goes in the audit log.';
    if (r.length > REASON_MAX) return `At most ${REASON_MAX} characters.`;
    return null;
  });
  protected readonly changed = computed(
    () => this.view.hasValue() && this.view.value().level !== this.level(),
  );
  protected readonly allows = computed(() => {
    if (!this.view.hasValue()) return '';
    return this.view.value().levels.find((l) => l.level === this.level())?.allows ?? '';
  });

  async save(): Promise<void> {
    this.tried.set(true);
    if (!this.changed() || this.reasonError() || this.saving() || !this.canManage()) return;
    if (!this.view.hasValue()) return;
    const before = this.view.value();
    const to = this.level();
    const ok = await this.confirm.confirm({
      title: `Set the options level of ${this.portfolioName()} to ${levelLabel(to)}?`,
      message:
        'Options still need the admin switch and a live stage. Every option order waits for your approval.',
      confirmLabel: 'Set options level',
      tone: 'danger',
      ticket: {
        kind: 'Options approval',
        live: true,
        lines: [
          { label: 'Portfolio', value: this.portfolioName() },
          { label: 'Now', value: levelLabel(before.level) },
          { label: 'New', value: levelLabel(to) },
          { label: 'Reason', value: this.reason().trim() },
        ],
      },
    });
    if (!ok) return;
    if (!(await this.stepUp.ensure('Set the options approval level'))) return;
    this.saving.set(true);
    try {
      const saved = await this.live.setOptionsApproval(this.portfolioId(), {
        level: to,
        reason: this.reason().trim(),
      });
      this.view.set(saved);
      this.reason.set('');
      this.tried.set(false);
      this.toasts.success(`The options level of ${this.portfolioName()} is ${levelLabel(to)}.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.saving.set(false);
    }
  }
}
