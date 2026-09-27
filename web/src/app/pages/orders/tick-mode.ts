import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { TickSummary } from '../../api/models';
import { ModeStamp } from '../../shared/ui/mode-stamp';

/** A rehearsal, or where the orders went. */
export type TickMode = 'dry' | 'paper' | 'live';

/**
 * How a trading run ran, from its summary: `dry_run` and `broker_mode`
 * (`paper` or `live`). Runs recorded before the server kept them give null,
 * and nothing is shown for them.
 */
export function tickMode(summary: TickSummary | null | undefined): TickMode | null {
  if (!summary) return null;
  if (summary['dry_run'] === true) return 'dry';
  const mode = summary['broker_mode'];
  if (mode === 'live' || mode === 'paper') return mode;
  return null;
}

/**
 * "Dry run" for a rehearsal, else the PAPER or LIVE stamp of the broker the
 * orders went to. Used by the trading run list and a run's page.
 *
 *   <app-tick-mode [summary]="t.summary" />
 */
@Component({
  selector: 'app-tick-mode',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ModeStamp],
  template: `
    @switch (mode()) {
      @case ('dry') {
        <span class="dry" title="Decided and sized orders without sending them">Dry run</span>
      }
      @case ('live') {
        <app-mode-stamp [live]="true" />
      }
      @case ('paper') {
        <app-mode-stamp [live]="false" />
      }
    }
  `,
  styles: `
    :host {
      display: inline-flex;
      align-items: center;
      vertical-align: middle;
    }
    :host:empty {
      display: none;
    }
    .dry {
      padding: 0 var(--space-2);
      border: 1px dashed var(--color-border-strong, var(--color-border));
      border-radius: var(--radius-xs);
      color: var(--color-ink-2);
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
      white-space: nowrap;
    }
  `,
})
export class TickModeTag {
  readonly summary = input<TickSummary | null | undefined>(null);
  protected readonly mode = computed(() => tickMode(this.summary()));
}
