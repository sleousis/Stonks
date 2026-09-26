import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/**
 * A rubber-stamp mark for where money moves: a grey "PAPER" for simulated
 * money, a brass "LIVE" for real money. Brass means real money everywhere in
 * the console, so use this (not a pill) for broker, portfolio and order
 * ticket modes.
 *
 *   <app-mode-stamp [live]="!broker.paper" />
 */
@Component({
  selector: 'app-mode-stamp',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<span class="stamp" [attr.data-mode]="live() ? 'live' : 'paper'"
    >{{ live() ? 'LIVE' : 'PAPER'
    }}<span class="visually-hidden">{{ live() ? ', real money' : ', simulated money' }}</span></span
  >`,
  styles: `
    :host {
      display: inline-flex;
      vertical-align: middle;
    }
    .stamp {
      padding: 0 var(--space-1);
      border: 1.5px solid var(--color-ink-3);
      border-radius: var(--radius-sm);
      color: var(--color-ink-2);
      font-size: var(--text-xs);
      font-weight: var(--weight-bold);
      letter-spacing: 0.08em;
      line-height: 1.5;
      transform: rotate(-2deg);
    }
    .stamp[data-mode='live'] {
      border-color: var(--color-brass);
      box-shadow: inset 0 0 0 1px var(--color-brass);
      color: var(--color-ink);
    }
  `,
})
export class ModeStamp {
  readonly live = input.required<boolean>();
}
