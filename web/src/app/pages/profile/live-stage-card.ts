import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  output,
  resource,
  signal,
} from '@angular/core';

import { LiveService } from '../../api/live.service';
import type { GateDayView, GateReportView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDate, formatDateTime, formatNumber, formatPercent } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import {
  CHECK_STATE_WORDS,
  type LiveStage,
  STAGES,
  checkLabel,
  checkState,
  dirtyReasons,
  lowerStages,
  stageWords,
} from '../../shared/live-stages';
import { PermissionNote } from '../../shared/ui/permission-note';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

const REASON_MAX = 500;

/** The gate's headline numbers, as label and value pairs. */
export function gateFigures(report: GateReportView): { label: string; value: string }[] {
  const m = report.metrics as Record<string, number | null | undefined>;
  const num = (v: number | null | undefined, digits = 0) =>
    v == null ? 'None yet' : formatNumber(v, { digits });
  const pct = (v: number | null | undefined) =>
    v == null ? 'None yet' : formatPercent(v, { digits: 1 });
  return [
    { label: 'Sessions', value: num(m['sessions']) },
    { label: 'Clean in a row', value: num(m['clean_streak']) },
    { label: 'Rejected', value: pct(m['reject_rate']) },
    {
      label: 'Cost gap',
      value: m['tca_gap_bps'] == null ? 'None yet' : `${num(m['tca_gap_bps'], 1)} bps`,
    },
    { label: 'Tracking error', value: pct(m['tracking_error']) },
  ];
}

/**
 * A live portfolio's stage (roadmap 19.9): the ladder from simulated paper
 * to real money, what moving up needs (the gate report, checked now), the
 * last sessions' gate metrics, moving down, and the stage changes.
 *
 * Moving up needs every check met, a reason, the next stage's name typed
 * again and a fresh code. Moving down needs a reason only: it only lowers
 * risk. A week that is not clean sends an alert and never moves the stage.
 */
@Component({
  selector: 'app-live-stage-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PermissionNote, StatusPill, ErrorState, LoadingState],
  templateUrl: './live-stage-card.html',
  styleUrl: './live-stage-card.scss',
})
export class LiveStageCard {
  private readonly live = inject(LiveService);
  private readonly confirm = inject(ConfirmService);
  private readonly stepUp = inject(StepUpService);
  private readonly toasts = inject(ToastService);
  private readonly session = inject(SessionService);

  readonly portfolioId = input.required<string>();
  readonly portfolioName = input.required<string>();
  /** The stage each time it loads or changes, so the page can show brass only for real money. */
  readonly stageChange = output<LiveStage>();

  protected readonly canManage = computed(() => this.session.can('live.manage'));
  protected readonly canTrade = computed(() => this.session.can('portfolio.trade'));
  protected readonly reasonMax = REASON_MAX;
  protected readonly stages = STAGES;
  protected readonly words = stageWords;
  protected readonly checkLabel = checkLabel;
  protected readonly checkState = checkState;
  protected readonly checkWords = CHECK_STATE_WORDS;
  protected readonly date = formatDate;
  protected readonly dateTime = formatDateTime;

  protected readonly stage = resource({
    params: () => ({ id: this.portfolioId() }),
    loader: ({ params }) => this.live.stage(params.id),
  });
  protected readonly report = resource({
    params: () => ({ id: this.portfolioId() }),
    loader: ({ params }) => this.live.gateReport(params.id),
  });

  protected readonly current = computed(() =>
    this.stage.hasValue() ? (this.stage.value().stage as LiveStage) : null,
  );
  private readonly announce = effect(() => {
    const s = this.current();
    if (s) this.stageChange.emit(s);
  });
  protected readonly currentIndex = computed(() => {
    const s = this.current();
    return s ? STAGES.indexOf(s) : -1;
  });
  protected readonly figures = computed(() =>
    this.report.hasValue() ? gateFigures(this.report.value()) : [],
  );
  /** Newest session first. */
  protected readonly days = computed<GateDayView[]>(() =>
    this.stage.hasValue() ? [...this.stage.value().days].reverse() : [],
  );
  protected readonly dirty = (day: GateDayView) => dirtyReasons(day);

  // ---- moving up ----------------------------------------------------------

  protected readonly upReason = signal('');
  protected readonly upTried = signal(false);
  protected readonly movingUp = signal(false);
  protected readonly upError = computed(() => {
    const r = this.upReason();
    if (!r.trim()) return 'Say why now. It is kept with the change.';
    if (r.length > REASON_MAX) return `At most ${REASON_MAX} characters.`;
    return null;
  });
  protected readonly canMoveUp = computed(
    () =>
      this.canManage() &&
      this.report.hasValue() &&
      this.report.value().passed &&
      this.report.value().target != null &&
      !this.movingUp(),
  );

  async moveUp(): Promise<void> {
    this.upTried.set(true);
    if (!this.canMoveUp() || this.upError() || !this.report.hasValue()) return;
    const report = this.report.value();
    const target = report.target as LiveStage;
    const to = stageWords(target);
    const ok = await this.confirm.confirm({
      title: `Move ${this.portfolioName()} to ${to.label}?`,
      message: to.live
        ? 'Real money moves from the next trading run, within the allocation you set.'
        : "Orders go to the broker's paper account from the next trading run.",
      confirmLabel: `Move to ${to.label}`,
      tone: to.live ? 'danger' : 'default',
      typedConfirmation: to.label,
      ticket: {
        kind: 'Stage change',
        live: to.live,
        lines: [
          { label: 'Portfolio', value: this.portfolioName() },
          { label: 'Now', value: stageWords(report.from_stage).label },
          { label: 'Next', value: to.label },
          { label: 'Checks', value: `${report.checks.filter((c) => c.passed).length} met` },
          { label: 'Reason', value: this.upReason().trim() },
        ],
      },
    });
    if (!ok) return;
    if (!(await this.stepUp.ensure('Move to the next stage'))) return;
    this.movingUp.set(true);
    try {
      const view = await this.live.promote(this.portfolioId(), {
        to_stage: target,
        reason: this.upReason().trim(),
        confirm: target,
      });
      this.stage.set(view);
      this.report.reload();
      this.upReason.set('');
      this.upTried.set(false);
      this.toasts.success(`${this.portfolioName()} is now at ${to.label}.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.movingUp.set(false);
    }
  }

  // ---- moving down --------------------------------------------------------

  protected readonly lower = computed(() => {
    const s = this.current();
    return s ? lowerStages(s) : [];
  });
  protected readonly downTo = linkedSignal<LiveStage | null>(() => {
    const options = this.lower();
    return options.length ? options[options.length - 1] : null;
  });
  protected readonly downReason = signal('');
  protected readonly downTried = signal(false);
  protected readonly movingDown = signal(false);
  protected readonly downError = computed(() => {
    const r = this.downReason();
    if (!r.trim()) return 'Say why. It is kept with the change.';
    if (r.length > REASON_MAX) return `At most ${REASON_MAX} characters.`;
    return null;
  });

  async moveDown(): Promise<void> {
    this.downTried.set(true);
    const target = this.downTo();
    if (!target || this.downError() || this.movingDown() || !this.canTrade()) return;
    const to = stageWords(target);
    const ok = await this.confirm.confirm({
      title: `Move ${this.portfolioName()} down to ${to.label}?`,
      message: to.live
        ? 'It keeps trading real money at the lower stage.'
        : 'New real-money orders stop. Sales that close positions still go out.',
      confirmLabel: `Move down to ${to.label}`,
      ticket: {
        kind: 'Stage change',
        // Real money when the lower stage still trades it (Real money, small).
        live: to.live,
        lines: [
          { label: 'Portfolio', value: this.portfolioName() },
          { label: 'Now', value: stageWords(this.current() ?? '').label },
          { label: 'Next', value: to.label },
          { label: 'Reason', value: this.downReason().trim() },
        ],
      },
    });
    if (!ok) return;
    this.movingDown.set(true);
    try {
      const view = await this.live.demote(this.portfolioId(), {
        to_stage: target,
        reason: this.downReason().trim(),
      });
      this.stage.set(view);
      this.report.reload();
      this.downReason.set('');
      this.downTried.set(false);
      this.toasts.success(`${this.portfolioName()} is now at ${to.label}.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.movingDown.set(false);
    }
  }

  protected onDownTo(value: string): void {
    this.downTo.set(value as LiveStage);
  }
}
