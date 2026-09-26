import { ChangeDetectionStrategy, Component, OnInit, inject } from '@angular/core';

import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';

/**
 * Which portfolio the money pages show. Sits in the session strip; renders
 * only when the user has more than one portfolio (and the server lists
 * them). A live portfolio carries a brass LIVE stamp, a paper one a grey
 * PAPER stamp.
 */
@Component({
  selector: 'app-portfolio-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
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
              {{ p.name }}{{ p.mode === 'live' ? ' (live)' : '' }}
            </option>
          }
        </select>
        @if (ctx.current()?.mode; as mode) {
          <span class="stamp" [attr.data-mode]="mode">{{
            mode === 'live' ? 'LIVE' : 'PAPER'
          }}</span>
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
    .stamp {
      padding: 0 var(--space-1);
      border: 1.5px solid var(--color-ink-3);
      border-radius: var(--radius-sm);
      color: var(--color-ink-2);
      font-size: var(--text-xs);
      font-weight: var(--weight-bold);
      letter-spacing: 0.08em;
      transform: rotate(-2deg);
    }
    .stamp[data-mode='live'] {
      border-color: var(--color-brass);
      box-shadow: inset 0 0 0 1px var(--color-brass);
      color: var(--color-ink);
    }
  `,
})
export class PortfolioPicker implements OnInit {
  protected readonly ctx = inject(PortfolioContextService);

  ngOnInit(): void {
    void this.ctx.load();
  }
}
