import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';

import { ModelVersionsService, type RetrainResultView } from '../../api/model-versions.service';
import { SessionService } from '../../core/auth/session.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { trainWindow } from '../model-versions';
import { strategyDisplayName } from '../strategy-names';
import { JobProgress, JobResult } from './job-progress';
import { PermissionNote } from './permission-note';
import { StatusPill } from './status-pill';

let nextId = 0;

/** "2 new candidates, 1 skipped." */
export function retrainSummary(r: RetrainResultView): string {
  const parts: string[] = [];
  if (r.candidates) {
    parts.push(`${r.candidates} new ${r.candidates === 1 ? 'candidate' : 'candidates'}`);
  }
  if (r.failed) parts.push(`${r.failed} failed`);
  if (r.skipped) parts.push(`${r.skipped} skipped`);
  return parts.length ? `${parts.join(', ')}.` : 'No strategy needed a new fit.';
}

/**
 * Start a retrain and follow it (roadmap 22.6). Each new fit becomes a
 * candidate that runs as a model book; nothing trades until a swap. With
 * `strategyId` it refits that strategy only, otherwise every strategy that
 * learns from data. `finished` fires once the job has ended.
 *
 *   <app-retrain-job [strategyId]="id" (finished)="versions.reload()" />
 */
@Component({
  selector: 'app-retrain-job',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [JobProgress, PermissionNote, StatusPill],
  template: `
    <form class="retrain" (submit)="$event.preventDefault(); start()">
      <div class="check-line">
        <input
          type="checkbox"
          [id]="id + '-force'"
          [checked]="force()"
          (change)="force.set($any($event.target).checked)"
        />
        <label [for]="id + '-force'">Refit even when fitted in the last few days</label>
      </div>
      <button
        type="submit"
        class="btn"
        [disabled]="!canRun() || running()"
        [attr.aria-busy]="running()"
      >
        {{ running() ? 'Retraining…' : label() }}
      </button>
    </form>
    @if (!canRun()) {
      <app-permission-note permission="lab.run" />
    }
    @if (run(); as h) {
      <app-job-progress label="Retrain" [handle]="h" [result]="result" />
    }
    @if (result.value(); as r) {
      <div class="outcome" role="status">
        <p>{{ summary(r) }}</p>
        @if (r.outcomes.length) {
          <ul class="outcomes" aria-label="What each strategy got">
            @for (o of r.outcomes; track o.strategy_id) {
              <li>
                <span class="who">{{ name(o.strategy_id) }}</span>
                <app-status-pill
                  [status]="o.status"
                  [label]="outcomeLabel(o.status, o.version)"
                  [tone]="o.status === 'candidate' ? 'info' : null"
                  [form]="o.status === 'candidate' ? 'receipt' : null"
                />
                @if (o.status === 'candidate') {
                  <span class="muted">Trained on {{ window(o) }}</span>
                } @else if (o.detail) {
                  <span class="muted">{{ o.detail }}</span>
                }
              </li>
            }
          </ul>
        }
      </div>
    }
  `,
  styles: `
    :host {
      display: grid;
      gap: var(--space-3);
      min-width: 0;
    }
    .retrain {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-4);
    }
    .check-line {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      min-height: var(--touch-min);
    }
    .check-line input {
      width: 1.25rem;
      height: 1.25rem;
      flex: none;
    }
    .retrain .btn {
      min-height: var(--touch-min);
    }
    .outcomes {
      display: grid;
      gap: var(--space-2);
      margin: var(--space-2) 0 0;
      padding: 0;
      list-style: none;
    }
    .outcomes li {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-3);
      overflow-wrap: anywhere;
    }
    .who {
      font-weight: var(--weight-medium);
    }
    .muted {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
  `,
})
export class RetrainJob {
  private readonly api = inject(ModelVersionsService);
  private readonly jobs = inject(JobsService);
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);
  private readonly destroyRef = inject(DestroyRef);

  /** Refit only this strategy; every retrainable strategy when unset. */
  readonly strategyId = input<string | null>(null);
  readonly label = input('Retrain now');
  readonly finished = output<RetrainResultView | null>();

  protected readonly id = `retrain-${nextId++}`;
  protected readonly force = signal(false);
  protected readonly run = signal<JobHandle | null>(null);
  protected readonly running = signal(false);
  protected readonly result = new JobResult<RetrainResultView>();
  protected readonly canRun = computed(() => this.session.can('lab.run'));

  protected readonly summary = retrainSummary;
  protected readonly name = (id: string) => strategyDisplayName(id);
  protected readonly window = trainWindow;

  protected outcomeLabel(status: string, version: number | null): string {
    if (status === 'candidate') return version ? `Candidate v${version}` : 'Candidate';
    if (status === 'skipped') return 'Skipped';
    if (status === 'failed') return 'Fit failed';
    return status;
  }

  async start(): Promise<void> {
    if (this.running() || !this.canRun()) return;
    this.running.set(true);
    this.result.reset();
    const id = this.strategyId();
    try {
      const job = await this.api.retrain({
        strategy_ids: id ? [id] : null,
        force: this.force(),
      });
      this.run()?.stop();
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.run.set(handle);
      const last = await handle.finished;
      let view: RetrainResultView | null = null;
      if (last?.status === 'succeeded') {
        view = await this.result.load(() => this.api.retrainResult(job.id));
        if (view) this.toasts.success(`Retrain finished. ${retrainSummary(view)}`);
      }
      this.finished.emit(view);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.running.set(false);
    }
  }
}
