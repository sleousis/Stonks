import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';

import { SessionService } from '../../core/auth/session.service';

/**
 * How a failed job or run ended: the plain message everyone reads, and for
 * admins the raw error under a Details disclosure. The server only sends
 * the raw error to admins, and this hides it from everyone else as well.
 *
 *   <app-job-failure [text]="h.error()" [detail]="h.errorDetail()" />
 */
@Component({
  selector: 'app-job-failure',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="job-error" role="alert">
      <p class="error">{{ text() }}</p>
      @if (shownDetail(); as d) {
        <details class="job-error-details">
          <summary>Details</summary>
          <code>{{ d }}</code>
        </details>
      }
    </div>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .job-error {
      display: grid;
      gap: var(--space-2);
      overflow-wrap: anywhere;
    }
    .error {
      margin: 0;
      font-size: var(--text-sm);
      color: var(--color-loss);
    }
    .job-error-details {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    summary {
      display: flex;
      align-items: center;
      min-height: var(--touch-min);
      cursor: pointer;
    }
    code {
      display: block;
      white-space: pre-wrap;
      font-size: var(--text-xs);
    }
  `,
})
export class JobFailure {
  private readonly session = inject(SessionService);
  /** The plain message, shown to everyone. */
  readonly text = input.required<string>();
  /** The raw error, shown to admins only. */
  readonly detail = input<string | null | undefined>(null);
  protected readonly shownDetail = computed(() =>
    this.session.isAdmin() ? this.detail()?.trim() || null : null,
  );
}
