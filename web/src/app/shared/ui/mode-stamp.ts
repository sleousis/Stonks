import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { paperStampLabel } from '../live-stages';

/**
 * A stamp for where money moves: a dashed "PAPER" for simulated money
 * ("BROKER PAPER" with `stage` at Broker paper), a solid brass "LIVE" for
 * real money, a muted "READ ONLY" for a real account Stonks only reads.
 * `size="sm"` is the 10px stamp for table rows. Brass means real money everywhere in
 * the console, so use this (not a pill) for broker, portfolio and order
 * ticket modes.
 *
 *   <app-mode-stamp [live]="!broker.paper" />
 *   <app-mode-stamp [live]="p.trading === 'live'" [stage]="p.live_stage" />
 */
@Component({
  selector: 'app-mode-stamp',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<span class="stamp" [attr.data-mode]="mode()"
    >{{ label() }}<span class="visually-hidden">{{ hidden() }}</span></span
  >`,
  host: { '[attr.data-size]': 'size()' },
  styles: `
    :host {
      display: inline-flex;
      vertical-align: middle;
    }
    /* Radius 3, 11px mono capitals (the labels are capitals): the one uppercase use. */
    .stamp {
      padding: 3px 6px 2px;
      border: 1px dashed var(--stamp-ink, var(--ink-2));
      border-radius: var(--r-stamp);
      background: transparent;
      color: var(--stamp-ink, var(--ink-2));
      font-family: var(--font-mono);
      font-size: var(--text-2xs);
      font-weight: var(--weight-semibold);
      letter-spacing: var(--tracking-stamp);
      line-height: 1;
      white-space: nowrap;
    }
    :host([data-size='sm']) .stamp {
      padding: 2px 4px 1px;
      font-size: 10px;
      letter-spacing: 0.1em;
    }
    /* Real money that Stonks only reads: solid and muted, never brass. */
    .stamp[data-mode='read-only'] {
      border-style: solid;
      border-color: var(--stamp-ink, var(--muted));
      color: var(--stamp-ink, var(--muted));
    }
    /* Real money: a solid brass stamp, the only one in the house metal. */
    .stamp[data-mode='live'] {
      border: 1px solid var(--brass-fill);
      background: var(--brass-fill);
      color: var(--on-brass);
    }
  `,
})
export class ModeStamp {
  readonly live = input.required<boolean>();
  /** The portfolio's stage: Broker paper reads "BROKER PAPER" in the paper style. */
  readonly stage = input<string | null | undefined>(null);
  /** Fed by a read-only connection or CSV imports: real money Stonks never trades. */
  readonly readOnly = input(false);
  /** `sm`: the 10px stamp for table rows. */
  readonly size = input<'md' | 'sm'>('md');
  protected readonly mode = computed(() =>
    this.live() ? 'live' : this.readOnly() ? 'read-only' : 'paper',
  );
  protected readonly label = computed(() =>
    this.live() ? 'LIVE' : this.readOnly() ? 'READ ONLY' : paperStampLabel(this.stage()),
  );
  protected readonly hidden = computed(() =>
    this.live()
      ? ', real money'
      : this.readOnly()
        ? ', real money, read only'
        : ', simulated money',
  );
}
