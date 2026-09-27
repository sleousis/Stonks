import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { formatPercent } from '../../core/format/format';
import type { JobHandle } from '../../core/jobs/jobs.service';
import { ErrorState } from './states';
import { StatusPill } from './status-pill';

let nextId = 0;

/** "Waiting for a worker…", the job's own message while it runs, then how it ended. */
export function jobText(h: JobHandle): string {
  const msg = h.message();
  switch (h.status()) {
    case 'running':
      return msg || 'Running…';
    case 'succeeded':
      return 'Finished.';
    case 'failed':
      return 'Failed.';
    case 'cancelled':
      return 'Cancelled.';
    default:
      return 'Waiting for a worker…';
  }
}

/**
 * The read of a finished job's result. GET errors are not toasted, so a
 * failed read is kept here and `<app-job-progress [result]>` shows it inline
 * with Try again, instead of a silent "Finished.".
 *
 *   protected readonly restored = new JobResult<RestoreResultView>();
 *   const view = await this.restored.load(() => this.ops.restoreResult(job.id));
 */
export class JobResult<T> {
  private readonly valueSignal = signal<T | null>(null);
  private readonly errorSignal = signal<unknown>(null);
  private readonly loadingSignal = signal(false);
  private loader: (() => Promise<T>) | null = null;

  /** The loaded result, or null. */
  readonly value = this.valueSignal.asReadonly();
  /** The last read's error, or null. */
  readonly error = this.errorSignal.asReadonly();
  readonly loading = this.loadingSignal.asReadonly();

  /** Reads the result; null when the read failed (the error is kept for Retry). */
  load(loader: () => Promise<T>): Promise<T | null> {
    this.loader = loader;
    return this.retry();
  }

  /** Reads again with the last loader. */
  async retry(): Promise<T | null> {
    const loader = this.loader;
    if (!loader) return null;
    this.errorSignal.set(null);
    this.loadingSignal.set(true);
    try {
      const value = await loader();
      this.valueSignal.set(value);
      return value;
    } catch (err) {
      this.errorSignal.set(err ?? new Error('The result could not load.'));
      return null;
    } finally {
      this.loadingSignal.set(false);
    }
  }

  /** Forget the last result and error (a new job starts). */
  reset(): void {
    this.loader = null;
    this.valueSignal.set(null);
    this.errorSignal.set(null);
  }
}

/**
 * A followed background job: status pill, message, a progress bar while it
 * runs and the error when it fails. Put a Cancel button in the `jobActions`
 * slot; pass a `JobResult` to show a failed result read with Try again.
 *
 *   <app-job-progress label="Backup" [handle]="run" [result]="backupResult" />
 */
@Component({
  selector: 'app-job-progress',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill, ErrorState],
  template: `
    @let h = handle();
    <div class="job" aria-live="polite">
      <div class="job-line">
        <app-status-pill [status]="h.status() ?? 'queued'" />
        <span class="job-message">{{ label() }}: {{ text() }}</span>
        <span class="job-actions"><ng-content select="[jobActions]" /></span>
      </div>
      @if (!h.done()) {
        <label class="visually-hidden" [for]="id">{{ label() }} progress</label>
        <progress [id]="id" max="1" [value]="h.progress()">{{ percent() }}</progress>
      }
      @if (h.error(); as e) {
        <p class="error" role="alert">{{ e }}</p>
      }
      @if (result()?.error(); as err) {
        <app-error-state
          [title]="label() + ' finished, but its result could not load'"
          [error]="err"
          (retry)="result()?.retry()"
        />
      }
      <ng-content />
    </div>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .job {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border-bottom: 1px solid var(--color-border);
      background: var(--color-surface-2);
    }
    .job-line {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
    .job-actions {
      margin-left: auto;
    }
    .job-actions:empty {
      display: none;
    }
    .job-message {
      min-width: 0;
      font-size: var(--text-sm);
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .error {
      font-size: var(--text-sm);
      color: var(--color-loss);
      overflow-wrap: anywhere;
    }
    progress {
      width: 100%;
      height: 8px;
      appearance: none;
      border: 0;
      border-radius: 999px;
      overflow: hidden;
      background: var(--color-surface-3);
      accent-color: var(--color-accent);
    }
    progress::-webkit-progress-bar {
      background: var(--color-surface-3);
    }
    progress::-webkit-progress-value {
      background: var(--color-accent);
    }
    progress::-moz-progress-bar {
      background: var(--color-accent);
    }
  `,
})
export class JobProgress {
  readonly handle = input.required<JobHandle>();
  readonly label = input('Job');
  /** The result read after the job, to show a failed read with Try again. */
  readonly result = input<JobResult<unknown> | null>(null);
  protected readonly id = `job-progress-${nextId++}`;
  protected readonly text = computed(() => jobText(this.handle()));
  protected readonly percent = computed(() =>
    formatPercent(this.handle().progress(), { digits: 0 }),
  );
}
