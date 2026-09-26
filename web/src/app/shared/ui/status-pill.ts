import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

export type PillTone = 'positive' | 'negative' | 'info' | 'progress' | 'warn' | 'neutral';

const TONES: Record<string, PillTone> = {
  active: 'positive',
  pass: 'positive',
  passed: 'positive',
  ok: 'positive',
  healthy: 'positive',
  succeeded: 'positive',
  success: 'positive',
  filled: 'positive',
  completed: 'positive',
  shadow: 'info',
  draft: 'info',
  pending: 'progress',
  queued: 'progress',
  running: 'progress',
  submitted: 'progress',
  retired: 'neutral',
  cancelled: 'neutral',
  canceled: 'neutral',
  skipped: 'neutral',
  disabled: 'neutral',
  warn: 'warn',
  warning: 'warn',
  stale: 'warn',
  partial: 'warn',
  fail: 'negative',
  failed: 'negative',
  error: 'negative',
  rejected: 'negative',
  unhealthy: 'negative',
};

/**
 * Status as text plus a shape, so it never relies on colour alone.
 *
 *   <app-status-pill [status]="s.status" />          active / shadow / retired
 *   <app-status-pill [status]="r.passed ? 'pass' : 'fail'" />
 */
@Component({
  selector: 'app-status-pill',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[attr.data-tone]': 'resolvedTone()' },
  template: `<span class="mark" aria-hidden="true"></span>{{ text() }}`,
  styles: `
    :host {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 1px 8px 1px 6px;
      border-radius: 999px;
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
      line-height: 18px;
      white-space: nowrap;
      background: var(--color-neutral-soft);
      color: var(--color-ink-2);
    }
    .mark {
      width: 7px;
      height: 7px;
      flex: none;
      border-radius: 50%;
      background: currentColor;
    }
    :host([data-tone='positive']) {
      background: var(--color-gain-soft);
      color: var(--color-gain);
    }
    :host([data-tone='negative']) {
      background: var(--color-loss-soft);
      color: var(--color-loss);
    }
    :host([data-tone='negative']) .mark {
      border-radius: 1px;
      transform: rotate(45deg);
    }
    :host([data-tone='info']) {
      background: var(--color-info-soft);
      color: var(--color-info);
    }
    :host([data-tone='info']) .mark {
      background: transparent;
      border: 1.5px solid currentColor;
    }
    :host([data-tone='progress']) {
      background: var(--color-brass-soft);
      color: var(--color-warn);
    }
    :host([data-tone='progress']) .mark {
      background: transparent;
      border: 1.5px dashed currentColor;
    }
    :host([data-tone='warn']) {
      background: var(--color-warn-soft);
      color: var(--color-warn);
    }
    :host([data-tone='warn']) .mark {
      border-radius: 0;
      clip-path: polygon(50% 0, 100% 100%, 0 100%);
    }
    :host([data-tone='neutral']) .mark {
      height: 2px;
      border-radius: 1px;
    }
  `,
})
export class StatusPill {
  readonly status = input.required<string | null | undefined>();
  /** Override the displayed text (the tone still comes from `status`). */
  readonly label = input<string | null>(null);
  /** Override the tone mapping. */
  readonly tone = input<PillTone | null>(null);

  protected readonly resolvedTone = computed<PillTone>(
    () => this.tone() ?? TONES[(this.status() ?? '').toLowerCase()] ?? 'neutral',
  );
  protected readonly text = computed(() => this.label() ?? this.status() ?? 'unknown');
}
