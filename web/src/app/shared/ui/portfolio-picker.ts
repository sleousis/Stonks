import { ChangeDetectionStrategy, Component, OnInit, inject } from '@angular/core';

import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { portfolioModeNote } from '../live-stages';
import { ModeStamp } from './mode-stamp';

/**
 * Which portfolio the money pages show. Sits in the session strip; renders
 * only when the user has more than one portfolio (and the server lists
 * them). One option per portfolio (UX-69): with no pick yet, the default
 * portfolio shows as chosen. A portfolio at a Real money stage carries a
 * brass LIVE stamp, a paper one a grey PAPER (or BROKER PAPER) stamp.
 */
@Component({
  selector: 'app-portfolio-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ModeStamp],
  template: `
    @if (ctx.hasChoice()) {
      <span class="picker">
        <label for="portfolio-picker">Portfolio</label>
        <select
          id="portfolio-picker"
          class="input"
          (change)="ctx.select($any($event.target).value)"
        >
          @if (!ctx.current()) {
            <option value="" selected>Pick a portfolio</option>
          }
          @for (p of ctx.options(); track p.id) {
            <option [value]="p.id" [selected]="p.id === ctx.current()?.id">
              {{ p.name }}{{ note(p) }}
            </option>
          }
        </select>
        @if (ctx.current(); as p) {
          <app-mode-stamp [live]="p.trading === 'live'" [stage]="p.live_stage" />
        }
      </span>
    }
  `,
  styles: `
    :host {
      display: contents;
    }
    .picker {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      min-width: 0;
      max-width: 100%;
    }
    label {
      color: var(--color-ink-3);
      font-size: var(--text-xs);
    }
    select {
      min-width: 0;
      max-width: 14rem;
      height: 28px;
      padding-block: 0;
      font-size: var(--text-sm);
    }
    @media (pointer: coarse), (max-width: 767.98px) {
      select {
        height: var(--touch-min);
        font-size: var(--text-lg);
      }
    }
  `,
})
export class PortfolioPicker implements OnInit {
  protected readonly ctx = inject(PortfolioContextService);
  protected readonly note = portfolioModeNote;

  ngOnInit(): void {
    void this.ctx.load();
  }
}
