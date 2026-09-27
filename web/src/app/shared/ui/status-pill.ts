import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { STATUS_WORDS } from '../governance-labels';
import { BrandMark } from './brand-mark';

export type PillTone = 'positive' | 'negative' | 'info' | 'progress' | 'warn' | 'neutral';

/**
 * How a status looks, by what kind of thing it describes, so a halted book,
 * a live strategy and a finished backtest never look alike:
 *
 * - `lamp`: a lifecycle state that lasts (active, shadow, retired). A round
 *   pill with a lamp dot.
 * - `receipt`: the outcome of something that finished (passed, filled,
 *   failed). A square outlined tag with a tick, cross or dash.
 * - `working`: something still going (queued, running). The brand mark
 *   drawing itself, no box.
 * - `alarm`: trading is stopped or broken (halted, unhealthy). A solid
 *   block that cannot be missed.
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
  imports: [BrandMark],
  host: { '[attr.data-tone]': 'resolvedTone()', '[attr.data-form]': 'resolvedForm()' },
  template: `@if (resolvedForm() === 'working') {
      <app-brand-mark class="spin" mode="loading" [size]="14" />
    } @else {
      <span class="mark" aria-hidden="true"></span>
    }
    {{ text() }}`,
  styles: `
    :host {
      --tone: var(--color-ink-2);
      --tone-soft: var(--color-neutral-soft);
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
      line-height: 18px;
      white-space: nowrap;
      color: var(--tone);
    }
    :host([data-tone='positive']) {
      --tone: var(--color-gain);
      --tone-soft: var(--color-gain-soft);
    }
    :host([data-tone='negative']) {
      --tone: var(--color-loss);
      --tone-soft: var(--color-loss-soft);
    }
    :host([data-tone='info']) {
      --tone: var(--color-info);
      --tone-soft: var(--color-info-soft);
    }
    :host([data-tone='progress']) {
      --tone: var(--color-accent);
      --tone-soft: var(--color-accent-soft);
    }
    :host([data-tone='warn']) {
      --tone: var(--color-warn);
      --tone-soft: var(--color-warn-soft);
    }
    .mark {
      flex: none;
    }

    /* Lamp: lifecycle. Round pill, a lamp dot (solid on, ring for shadow, a
       dash when off). */
    :host([data-form='lamp']) {
      padding: 1px 9px 1px 7px;
      border-radius: var(--radius-pill);
      background: var(--tone-soft);
    }
    :host([data-form='lamp']) .mark {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 0 2px color-mix(in srgb, currentColor 25%, transparent);
    }
    :host([data-form='lamp'][data-tone='info']) .mark {
      background: transparent;
      border: 1.5px solid currentColor;
      box-shadow: none;
    }
    :host([data-form='lamp'][data-tone='neutral']) .mark {
      height: 2px;
      border-radius: 1px;
      box-shadow: none;
    }

    /* Receipt: an outcome. Square outlined tag with a tick, cross, dash or
       bang drawn in CSS. */
    :host([data-form='receipt']) {
      padding: 0 7px 0 5px;
      border: 1px solid currentColor;
      border-radius: var(--radius-xs);
      background: transparent;
    }
    :host([data-form='receipt']) .mark {
      width: 9px;
      height: 9px;
      position: relative;
    }
    :host([data-form='receipt'][data-tone='positive']) .mark {
      width: 5px;
      height: 9px;
      margin: 0 2px 2px;
      border-right: 2px solid currentColor;
      border-bottom: 2px solid currentColor;
      transform: rotate(45deg);
    }
    :host([data-form='receipt'][data-tone='negative']) .mark,
    :host([data-form='receipt'][data-tone='neutral']) .mark {
      background: linear-gradient(currentColor, currentColor) center / 100% 2px no-repeat;
    }
    :host([data-form='receipt'][data-tone='negative']) .mark {
      transform: rotate(45deg);
      background:
        linear-gradient(currentColor, currentColor) center / 100% 2px no-repeat,
        linear-gradient(currentColor, currentColor) center / 2px 100% no-repeat;
    }
    :host([data-form='receipt'][data-tone='warn']) .mark,
    :host([data-form='receipt'][data-tone='info']) .mark,
    :host([data-form='receipt'][data-tone='progress']) .mark {
      width: 8px;
      background: currentColor;
      clip-path: polygon(50% 0, 100% 100%, 0 100%);
    }

    /* Working: no box, the mark draws itself. */
    :host([data-form='working']) {
      padding: 1px 0;
    }

    /* Alarm: a solid block. */
    :host([data-form='alarm']) {
      padding: 1px 8px 1px 6px;
      border-radius: var(--radius-xs);
      background: var(--tone);
      color: var(--color-surface);
      font-weight: var(--weight-bold);
    }
    :host([data-form='alarm']) .mark {
      width: 8px;
      height: 8px;
      background: currentColor;
      transform: rotate(45deg);
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
