import { ChangeDetectionStrategy, Component, OnInit, inject } from '@angular/core';

import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { portfolioModeNote } from '../live-stages';

interface PortfolioMode {
  kind?: string;
  trading: string;
  live_stage?: string | null;
}

/**
 * A broker portfolio that neither trades real money nor sits at Broker
 * paper is a real account Stonks only reads: a read-only connection or CSV
 * imports (DEGIRO). It is never paper money.
 */
export function isReadOnly(p: PortfolioMode): boolean {
  return p.kind === 'broker' && p.trading !== 'live' && p.live_stage !== 'broker_paper';
}

function modeNote(p: PortfolioMode): string {
  return isReadOnly(p) ? ' (read only)' : portfolioModeNote(p);
}
import { ModeStamp } from './mode-stamp';

/**
 * Which portfolio the money pages show, at the start of the session rail:
 * the portfolio's stamp (a brass LIVE at a Real money stage, a dashed PAPER
 * or BROKER PAPER, a READ ONLY for a real account Stonks only reads), its
 * name and a caret. With more than one portfolio a native select lies over
 * it, so the keyboard, screen readers and phones get the system picker
 * (UX-69). With one it shows the stamp and the name, and nothing to pick.
 */
@Component({
  selector: 'app-portfolio-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ModeStamp],
  template: `
    @let p = ctx.current();
    @if (ctx.hasChoice()) {
      <span class="picker" [class.live]="p?.trading === 'live'">
        @if (p) {
          <app-mode-stamp
            [live]="p.trading === 'live'"
            [stage]="p.live_stage"
            [readOnly]="readOnly(p)"
          />
        }
        <span class="name" aria-hidden="true">{{ p?.name ?? 'Pick a portfolio' }}</span>
        <svg
          class="caret"
          viewBox="0 0 12 12"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          aria-hidden="true"
        >
          <path d="m3 4.5 3 3 3-3" />
        </svg>
        <label for="portfolio-picker" class="visually-hidden">Portfolio</label>
        <select id="portfolio-picker" (change)="ctx.select($any($event.target).value)">
          @if (!p) {
            <option value="" selected>Pick a portfolio</option>
          }
          @for (o of ctx.options(); track o.id) {
            <option [value]="o.id" [selected]="o.id === p?.id">{{ o.name }}{{ note(o) }}</option>
          }
        </select>
      </span>
    } @else if (p) {
      <span class="picker single">
        <app-mode-stamp
          [live]="p.trading === 'live'"
          [stage]="p.live_stage"
          [readOnly]="readOnly(p)"
        />
        <span class="name">{{ p.name }}</span>
      </span>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: contents;
    }
    .picker {
      position: relative;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      max-width: 100%;
      height: 34px;
      padding: 0 10px 0 var(--space-1);
      border: 1px solid var(--chrome-rule);
      border-radius: var(--r-ctl);
      color: var(--chrome-ink);
      font-size: 0.8125rem;
      font-weight: var(--weight-semibold);
    }
    .picker:not(.single):hover {
      border-color: color-mix(in srgb, var(--chrome-rule) 50%, var(--chrome-muted));
    }
    .picker:has(select:focus-visible) {
      outline: 2px solid var(--chrome-accent);
      outline-offset: 2px;
    }
    .single {
      padding-right: var(--space-3);
    }
    .name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .caret {
      flex: none;
      width: 12px;
      height: 12px;
      color: var(--chrome-muted);
    }
    /* The real control: invisible, over the whole button. */
    select {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      margin: 0;
      opacity: 0;
      font-size: var(--text-lg);
      cursor: pointer;
    }
    select option {
      color: var(--ink);
      background: var(--raise);
    }
    @include bp.phone {
      .picker {
        width: 100%;
        height: var(--touch-min);
      }
      .caret {
        margin-left: auto;
      }
    }
  `,
})
export class PortfolioPicker implements OnInit {
  protected readonly ctx = inject(PortfolioContextService);
  protected readonly note = modeNote;
  protected readonly readOnly = isReadOnly;

  ngOnInit(): void {
    void this.ctx.load();
  }
}
