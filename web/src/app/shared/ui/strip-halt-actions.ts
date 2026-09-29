import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';

import { HaltsService, RESUME_CONFIRMATION } from '../../api/halts.service';
import type { HaltView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { formatDateTime } from '../../core/format/format';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { killStopsText } from '../../core/halts/kill-ticket';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { ResumeSheet } from '../../pages/ops/resume-sheet';
import { StatusChangeDialog } from './status-change-dialog';

/**
 * The trader's way out of a halt, right on the session strip (F11). The
 * Halts page is an admin page, so a trader resumes their own kill switch
 * (the same ticket, typed words and code as on that page) or clears their
 * own breaker here. A halt only an admin may end says so instead.
 */
@Component({
  selector: 'app-strip-halt-actions',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ResumeSheet, StatusChangeDialog],
  template: `
    @if (kills().length) {
      @if (resumable().length) {
        <button
          type="button"
          class="stop resume"
          [disabled]="busy()"
          [attr.aria-busy]="busy()"
          (click)="resume()"
        >
          <span class="stop-mark" aria-hidden="true"></span>
          Resume
        </button>
      } @else {
        <span class="note">Only an admin can resume.</span>
      }
    } @else if (breakers().length) {
      @if (clearable().length) {
        <button type="button" class="btn clear" [disabled]="busy()" (click)="clear()">
          Clear halt
        </button>
      } @else {
        <span class="note">Only an admin can clear it.</span>
      }
    }
    <app-resume-sheet />
    <app-status-change-dialog />
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: inline-flex;
      align-items: center;
    }
    .note {
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
    }
    .stop {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      min-height: 32px;
      padding: 0 var(--space-3);
      border: 1.5px solid var(--color-loss);
      border-radius: var(--radius-sm);
      background: var(--color-loss);
      color: var(--color-surface);
      font: inherit;
      font-weight: var(--weight-bold);
      white-space: nowrap;
      cursor: pointer;
    }
    .stop-mark {
      flex: none;
      width: 0;
      height: 0;
      border-block: 5px solid transparent;
      border-left: 9px solid currentColor;
    }
    @include bp.coarse {
      .stop,
      .clear {
        min-height: var(--touch-min);
      }
    }
    @include bp.phone {
      .stop,
      .clear {
        min-height: var(--touch-min);
      }
    }
  `,
})
export class StripHaltActions {
  private readonly state = inject(HaltStateService);
  private readonly api = inject(HaltsService);
  private readonly session = inject(SessionService);
  private readonly stepUp = inject(StepUpService);
  private readonly toasts = inject(ToastService);
  private readonly portfolios = inject(PortfolioContextService);
  private readonly sheet = viewChild.required(ResumeSheet);
  private readonly dialog = viewChild.required(StatusChangeDialog);

  protected readonly busy = signal(false);

  protected readonly kills = this.state.kills;
  protected readonly breakers = computed(() =>
    this.state.active().filter((h) => h.active && h.kind !== 'kill'),
  );
  /** Kill switches this person may lift: their own, never a global one. */
  protected readonly resumable = computed(() =>
    this.session.can('killswitch.resume') ? this.kills().filter((h) => h.scope !== 'global') : [],
  );
  protected readonly clearable = computed(() =>
    this.session.can('risk.reset') ? this.breakers().filter((h) => h.scope !== 'global') : [],
  );

  protected async resume(): Promise<void> {
    const halts = this.resumable();
    if (!halts.length || this.busy()) return;
    const names = this.state.scopeText();
    const sheet = this.sheet();
    const opened = sheet.open({
      live: halts.some((h) => this.isLive(h)),
      lines: [
        { label: 'Scope', value: [...new Set(halts.map(names))].join(', ') },
        { label: 'Starts again', value: killStopsText(halts.every((h) => h.halt === 'buys')) },
        { label: 'Stopped since', value: formatDateTime(earliest(halts)) },
        { label: 'Why it stopped', value: halts[0].reason || 'None given' },
      ],
    });
    // Roadmap 23.15: the resume checks show above the typed words. With
    // several switches the first one's checks show; the server checks each.
    this.api.resumeChecks(halts[0].id).then(
      (checks) => sheet.setChecks(checks),
      () => sheet.setChecks('error'),
    );
    const answer = await opened;
    if (answer === null) return;
    if (!(await this.stepUp.ensure('Resume trading'))) return;
    await this.run(async () => {
      for (const h of halts) {
        await this.api.resume(h.id, {
          confirmation: RESUME_CONFIRMATION,
          reason: answer.reason,
          ...(answer.overrideChecks ? { override_checks: true } : {}),
        });
      }
      this.toasts.success('Resumed trading.');
    });
  }

  protected async clear(): Promise<void> {
    const halts = this.clearable();
    if (!halts.length || this.busy()) return;
    const body = await this.dialog().open({
      title: halts.length === 1 ? 'Clear the halt?' : `Clear ${halts.length} halts?`,
      message:
        'New orders go out again from the next trading run, unless another halt still applies.',
      confirmLabel: halts.length === 1 ? 'Clear halt' : 'Clear halts',
      tone: halts.some((h) => this.isLive(h)) ? 'danger' : 'default',
      minReason: 1,
      reasonHint: 'Kept in the audit log and the status history.',
    });
    if (!body) return;
    await this.run(async () => {
      for (const h of halts) await this.api.clear(h.id, { reason: body.reason ?? '' });
      this.toasts.success(halts.length === 1 ? 'Cleared the halt.' : 'Cleared the halts.');
    });
  }

  private async run(work: () => Promise<void>): Promise<void> {
    this.busy.set(true);
    try {
      await work();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
      await this.state.refresh();
    }
  }

  private isLive(h: HaltView): boolean {
    const options = this.portfolios.options();
    if (h.scope === 'portfolio') {
      return options.some((p) => p.id === h.portfolio_id && p.trading === 'live');
    }
    return options.some((p) => p.trading === 'live');
  }
}

function earliest(halts: readonly HaltView[]): string {
  return halts.map((h) => h.tripped_at).sort()[0];
}
