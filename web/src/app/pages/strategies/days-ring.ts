import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/**
 * Trial days the go-live check asks for by default (`min_days` in
 * production/golive.py). A strategy's own report may ask for more.
 */
export const TRIAL_DAYS_TARGET = 63;

const R = 6;
const C = 2 * Math.PI * R;

/**
 * Days on trial as a 16px ring (stroke 2.5) beside the count: the arc fills
 * toward the days the go-live check asks for. The figure carries the
 * meaning, the ring is decoration.
 *
 *   <app-days-ring [days]="row.paper_days" />
 */
@Component({
  selector: 'app-days-ring',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[class.full]': 'share() >= 1' },
  template: `
    @if (days() !== null) {
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <circle class="track" cx="8" cy="8" [attr.r]="r" />
        <circle
          class="arc"
          cx="8"
          cy="8"
          [attr.r]="r"
          [attr.stroke-dasharray]="c"
          [attr.stroke-dashoffset]="offset()"
        />
      </svg>
      <span class="num">{{ days() }}</span>
    } @else {
      <span class="num muted">–</span>
    }
  `,
  styles: `
    :host {
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    svg {
      flex: none;
      width: 16px;
      height: 16px;
      transform: rotate(-90deg);
    }
    circle {
      fill: none;
      stroke-width: 2.5;
    }
    .track {
      stroke: var(--rule);
    }
    .arc {
      stroke: var(--accent);
      stroke-linecap: round;
    }
  `,
})
export class DaysRing {
  readonly days = input<number | null>(null);
  readonly target = input(TRIAL_DAYS_TARGET);

  protected readonly r = R;
  protected readonly c = C.toFixed(2);
  protected readonly share = computed(() =>
    Math.min(1, Math.max(0, (this.days() ?? 0) / Math.max(1, this.target()))),
  );
  protected readonly offset = computed(() => (C * (1 - this.share())).toFixed(2));
}
