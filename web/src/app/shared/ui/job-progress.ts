import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { formatPercent } from '../../core/format/format';
import type { JobHandle } from '../../core/jobs/jobs.service';
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
 * A followed background job: status pill, message, a progress bar while it
 * runs and the error when it fails.
 *
 *   <app-job-progress label="Backup" [handle]="run" />
 */
@Component({
  selector: 'app-job-progress',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill],
  template: `
    @let h = handle();
    <div class="job" aria-live="polite">
      <div class="job-line">
        <app-status-pill [status]="h.status() ?? 'queued'" />
        <span class="job-message">{{ label() }}: {{ text() }}</span>
      </div>
      @if (!h.done()) {
        <label class="visually-hidden" [for]="id">{{ label() }} progress</label>
        <progress [id]="id" max="1" [value]="h.progress()">{{ percent() }}</progress>
      }
      @if (h.error(); as e) {
        <p class="error" role="alert">{{ e }}</p>
      }
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
  protected readonly id = `job-progress-${nextId++}`;
  protected readonly text = computed(() => jobText(this.handle()));
  protected readonly percent = computed(() =>
    formatPercent(this.handle().progress(), { digits: 0 }),
  );
}
