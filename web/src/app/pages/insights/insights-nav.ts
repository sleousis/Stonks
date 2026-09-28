import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

/** The insights screens, as links so each has its own address. */
export const INSIGHTS_SECTIONS = [
  { path: '/insights', label: 'Overview', exact: true },
  { path: '/insights/risk', label: 'Risk', exact: false },
  { path: '/insights/cash-flows', label: 'Cash flows', exact: false },
  { path: '/insights/tax', label: 'Tax', exact: false },
] as const;

@Component({
  selector: 'app-insights-nav',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav class="section-nav" aria-label="Insights screens">
      @for (s of sections; track s.path) {
        <a
          [routerLink]="s.path"
          routerLinkActive="active"
          ariaCurrentWhenActive="page"
          [routerLinkActiveOptions]="{ exact: s.exact }"
          >{{ s.label }}</a
        >
      }
    </nav>
  `,
  styles: `
    @use 'breakpoints' as bp;

    /* Underline tabs, the same as Notifications: sections of one page. */
    :host {
      display: block;
      min-width: 0;
    }
    .section-nav {
      display: flex;
      gap: var(--space-1);
      margin: calc(-1 * var(--space-2)) 0 var(--space-4);
      border-bottom: 1px solid var(--color-border);
      overflow-x: auto;
      scrollbar-width: none;
    }
    a {
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      padding: 0 var(--space-3);
      margin-bottom: -1px;
      border-bottom: 2px solid transparent;
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
      text-decoration: none;
      white-space: nowrap;
    }
    a:hover {
      color: var(--color-ink);
    }
    a.active {
      color: var(--color-ink);
      border-bottom-color: var(--color-accent);
    }
    @include bp.phone {
      a {
        flex: 1 1 auto;
        justify-content: center;
        min-height: var(--touch-min);
      }
    }
    @include bp.coarse {
      a {
        min-height: var(--touch-min);
      }
    }
  `,
})
export class InsightsNav {
  protected readonly sections = INSIGHTS_SECTIONS;
}
