import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { paperStampLabel } from '../live-stages';

/**
 * A rubber-stamp mark for where money moves: a grey "PAPER" for simulated
 * money ("BROKER PAPER" with `stage` at Broker paper), a brass "LIVE" for
 * real money, a neutral grey "READ ONLY" for a real account Stonks only
 * reads. Brass means real money everywhere in
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
  styles: `
    :host {
      display: inline-flex;
      vertical-align: middle;
    }
    .stamp {
      padding: 1px var(--space-2) 0;
      border: 1.5px dashed var(--color-paper);
      border-radius: var(--radius-xs);
      color: var(--color-paper);
      font-family: var(--font-display);
      font-stretch: var(--display-stretch);
      font-size: var(--text-xs);
      font-weight: var(--weight-bold);
      letter-spacing: var(--tracking-stamp);
      line-height: 1.45;
      transform: rotate(-3deg);
    }
    /* Real money that Stonks only reads: neutral grey, solid, never brass. */
    .stamp[data-mode='read-only'] {
      border-style: solid;
      border-color: var(--color-ink-3);
      color: var(--color-ink-2);
    }
    /* Real money: a double brass rule, the only stamp in the house metal. */
    .stamp[data-mode='live'] {
      border: 2px solid var(--color-live);
      outline: 1px solid var(--color-live);
      outline-offset: 1px;
      color: var(--color-live);
    }
  `,
})
export class ModeStamp {
  readonly live = input.required<boolean>();
  /** The portfolio's stage: Broker paper reads "BROKER PAPER" in the paper style. */
  readonly stage = input<string | null | undefined>(null);
  /** Fed by a read-only connection or CSV imports: real money Stonks never trades. */
  readonly readOnly = input(false);
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
