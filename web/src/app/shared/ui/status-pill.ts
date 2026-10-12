import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { STATUS_WORDS } from '../governance-labels';

export type PillTone = 'positive' | 'negative' | 'info' | 'progress' | 'warn' | 'neutral';

/**
 * How a status looks, by what kind of thing it describes, so a halted book,
 * a live strategy and a finished backtest never look alike:
 *
 * - `lamp`: a lifecycle state that lasts (active, shadow, retired);
 * - `receipt`: the outcome of something that finished (passed, filled,
 *   failed);
 * - `working`: something still going (queued, running): a turning ring;
 * - `alarm`: trading is stopped or broken (halted, unhealthy): solid red.
 *
 * Every form is a 22px pill in its tone's soft colour with a glyph: a check
 * (approved, passed), a clock (on trial), a dash (retired), a cross
 * (failed), a triangle (warning).
 */
export type PillForm = 'lamp' | 'receipt' | 'working' | 'alarm';

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
  done: 'positive',
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
  paused: 'neutral',
  // Not started yet, or not needed: neutral receipts, never red.
  to_do: 'neutral',
  not_yet: 'neutral',
  optional: 'neutral',
  waiting: 'neutral',
  waiting_admin: 'neutral',
  warn: 'warn',
  warning: 'warn',
  stale: 'warn',
  partial: 'warn',
  fail: 'negative',
  failed: 'negative',
  error: 'negative',
  rejected: 'negative',
  unhealthy: 'negative',
  halted: 'negative',
  tripped: 'negative',
};

const FORMS: Record<string, PillForm> = {
  active: 'lamp',
  shadow: 'lamp',
  draft: 'lamp',
  retired: 'lamp',
  disabled: 'lamp',
  paused: 'lamp',
  healthy: 'lamp',
  pending: 'working',
  queued: 'working',
  running: 'working',
  submitted: 'working',
  unhealthy: 'alarm',
  halted: 'alarm',
  tripped: 'alarm',
};

/**
 * Trader words for statuses that would otherwise show a system key: a
 * strategy's lifecycle (UX-09) and a finished check's outcome.
 */
export const PILL_WORDS: Readonly<Record<string, string>> = {
  ...STATUS_WORDS,
  draft: 'Draft',
  pass: 'Passed',
  passed: 'Passed',
  fail: 'Failed',
  failed: 'Failed',
  succeeded: 'Done',
  filled: 'Filled',
  running: 'Running',
  queued: 'Waiting',
  cancelled: 'Cancelled',
  done: 'Done',
  to_do: 'To do',
  not_yet: 'Not yet',
  optional: 'Optional',
  waiting: 'Waiting',
  waiting_admin: 'Waiting for an admin',
};

/** The form for a status; outcomes (passed, filled, failed...) are receipts. */
export function pillForm(status: string | null | undefined, tone: PillTone): PillForm {
  const key = (status ?? '').toLowerCase();
  if (FORMS[key]) return FORMS[key];
  if (key in TONES) return 'receipt';
  return tone === 'progress' ? 'working' : 'lamp';
}

/**
 * Status as text plus a shape, so it never relies on colour alone.
 *
 *   <app-status-pill [status]="s.status" />   Live / Paper trading / Stopped
 *   <app-status-pill [status]="r.passed ? 'pass' : 'fail'" />
 */
@Component({
  selector: 'app-status-pill',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[attr.data-tone]': 'resolvedTone()', '[attr.data-form]': 'resolvedForm()' },
  template: `@if (resolvedForm() === 'working') {
      <span class="ring" aria-hidden="true"></span>
    } @else {
      <span class="mark" aria-hidden="true"></span>
    }
    {{ text() }}`,
  styles: `
    /* A 22px pill with a 12px glyph: the glyph's shape carries the state, so
       it never rests on colour alone. */
    :host {
      --tone: var(--muted);
      --tone-soft: var(--sunk);
      display: inline-flex;
      align-items: center;
      gap: 5px;
      height: 22px;
      padding: 0 9px 0 7px;
      border-radius: var(--r-pill);
      background: var(--tone-soft);
      color: var(--tone);
      font-size: var(--text-xs);
      font-weight: var(--weight-label);
      line-height: 1;
      white-space: nowrap;
    }
    :host([data-tone='positive']) {
      --tone: var(--up);
      --tone-soft: var(--up-soft);
    }
    :host([data-tone='negative']) {
      --tone: var(--down);
      --tone-soft: var(--down-soft);
    }
    :host([data-tone='info']),
    :host([data-tone='progress']) {
      --tone: var(--accent);
      --tone-soft: var(--accent-soft);
    }
    :host([data-tone='warn']) {
      --tone: var(--warn);
      --tone-soft: var(--warn-soft);
    }
    .mark,
    .ring {
      position: relative;
      flex: none;
      width: 12px;
      height: 12px;
    }
    /* Positive: a check. */
    :host([data-tone='positive']) .mark::before {
      content: '';
      position: absolute;
      left: 4px;
      top: 1.5px;
      width: 3.5px;
      height: 7px;
      border-right: 1.75px solid currentColor;
      border-bottom: 1.75px solid currentColor;
      transform: rotate(45deg);
    }
    /* Info (on trial, draft): a clock. */
    :host([data-tone='info']) .mark {
      border: 1.5px solid currentColor;
      border-radius: 50%;
    }
    :host([data-tone='info']) .mark::before {
      content: '';
      position: absolute;
      left: 3.75px;
      top: 1.5px;
      width: 2.5px;
      height: 3.25px;
      border-left: 1.5px solid currentColor;
      border-bottom: 1.5px solid currentColor;
    }
    /* Neutral (retired, cancelled, to do): a dash. */
    :host([data-tone='neutral']) .mark {
      width: 9px;
      height: 1.75px;
      margin: 0 1.5px;
      border-radius: 1px;
      background: currentColor;
    }
    /* Negative: a cross. */
    :host([data-tone='negative']) .mark {
      background:
        linear-gradient(currentColor, currentColor) center / 100% 1.75px no-repeat,
        linear-gradient(currentColor, currentColor) center / 1.75px 100% no-repeat;
      transform: rotate(45deg) scale(0.85);
    }
    /* Warn and progress receipts: a small triangle. */
    :host([data-tone='warn']) .mark,
    :host([data-tone='progress']) .mark {
      width: 11px;
      background: currentColor;
      clip-path: polygon(50% 5%, 100% 95%, 0 95%);
    }
    /* Working (running, queued): a three-quarter ring that turns. */
    .ring {
      border: 1.75px solid currentColor;
      border-right-color: transparent;
      border-radius: 50%;
      animation: pill-spin 0.9s linear infinite;
    }
    @keyframes pill-spin {
      to {
        transform: rotate(360deg);
      }
    }
    @media (prefers-reduced-motion: reduce) {
      .ring {
        animation: none;
      }
    }
    /* Alarm (halted, unhealthy): a solid red pill that cannot be missed. */
    :host([data-form='alarm']) {
      background: var(--down);
      color: var(--on-down);
      font-weight: var(--weight-bold);
    }
    :host([data-form='alarm']) .mark {
      width: 8px;
      height: 8px;
      margin: 0 2px;
      border-radius: 1px;
      background: currentColor;
      transform: none;
    }
  `,
})
export class StatusPill {
  readonly status = input.required<string | null | undefined>();
  /** Override the displayed text (the tone still comes from `status`). */
  readonly label = input<string | null>(null);
  /** Override the tone mapping. */
  readonly tone = input<PillTone | null>(null);
  /** Override the form (see `PillForm`). */
  readonly form = input<PillForm | null>(null);

  protected readonly resolvedTone = computed<PillTone>(
    () => this.tone() ?? TONES[(this.status() ?? '').toLowerCase()] ?? 'neutral',
  );
  protected readonly resolvedForm = computed<PillForm>(
    () => this.form() ?? pillForm(this.status(), this.resolvedTone()),
  );
  protected readonly text = computed(() => {
    const status = this.status();
    return this.label() ?? PILL_WORDS[(status ?? '').toLowerCase()] ?? status ?? 'unknown';
  });
}
