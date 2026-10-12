import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TRIAL_DAYS_TARGET } from './days-ring';

/** Ticks on the meter, whatever the target: each stands for a share of it. */
export const METER_TICKS = 20;

/**
 * Days on trial against the days the go-live check asks for, as 20 ticks
 * (10x14, radius 2): accent when reached, a hairline colour when not, with
 * "41 of 63" in mono beside them.
 *
 *   <app-trial-meter [days]="row.days" [target]="63" />
 */
@Component({
  selector: 'app-trial-meter',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span class="ticks" aria-hidden="true">
      @for (on of ticks(); track $index) {
        <span class="tick" [class.on]="on"></span>
      }
    </span>
    <span class="num words">{{ days() }} of {{ target() }}</span>
    <span class="visually-hidden">trial days</span>
  `,
  styles: `
    :host {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      white-space: nowrap;
    }
    .ticks {
      display: inline-flex;
      gap: 2px;
    }
    .tick {
      width: 4px;
      height: 14px;
      border-radius: 2px;
      background: var(--rule);
    }
    .tick.on {
      background: var(--accent);
    }
    .words {
      color: var(--ink-2);
      font-size: var(--text-sm);
    }
    @media (min-width: 1100px) {
      .tick {
        width: 5px;
      }
    }
  `,
})
export class TrialMeter {
  readonly days = input.required<number>();
  readonly target = input(TRIAL_DAYS_TARGET);

  protected readonly ticks = computed(() => {
    const share = Math.min(1, Math.max(0, this.days() / Math.max(1, this.target())));
    const lit = Math.round(share * METER_TICKS);
    return Array.from({ length: METER_TICKS }, (_, i) => i < lit);
  });
}
