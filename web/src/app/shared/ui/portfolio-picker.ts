import { ChangeDetectionStrategy, Component, OnInit, inject } from '@angular/core';

import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { ModeStamp } from './mode-stamp';

/**
 * Which portfolio the money pages show. Sits in the session strip; renders
 * only when the user has more than one portfolio (and the server lists
 * them). A live portfolio carries a brass LIVE stamp, a paper one a grey
 * PAPER stamp.
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
          [value]="ctx.selectedId() ?? ''"
          (change)="ctx.select($any($event.target).value)"
        >
          <option value="">My default portfolio</option>
          @for (p of ctx.options(); track p.id) {
            <option [value]="p.id" [selected]="p.id === ctx.selectedId()">
              {{ p.name }}{{ p.trading === 'live' ? ' (live)' : '' }}
            </option>
          }
        </select>
        @if (ctx.current()?.trading; as mode) {
          <app-mode-stamp [live]="mode === 'live'" />
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

  ngOnInit(): void {
    void this.ctx.load();
  }
}
