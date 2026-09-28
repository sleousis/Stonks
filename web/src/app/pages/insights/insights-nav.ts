import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

/** The insights screens, as links so each has its own address. */
export const INSIGHTS_SECTIONS = [
  { path: '/insights', label: 'Overview', exact: true },
  { path: '/insights/risk', label: 'Risk', exact: false },
  { path: '/insights/cash-flows', label: 'Cash flows', exact: false },
  { path: '/insights/tax', label: 'Tax', exact: false },
  { path: '/insights/behaviour', label: 'Behaviour', exact: false },
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
    .section-nav {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
      margin-bottom: var(--space-4);
    }
    a {
      display: inline-flex;
      align-items: center;
      min-height: 2.25rem;
      padding: 0 var(--space-3);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      color: var(--color-ink-2);
      text-decoration: none;
      font-weight: 500;
      @include bp.phone {
        min-height: var(--touch-min);
      }
      @include bp.coarse {
        min-height: var(--touch-min);
      }
    }
    a:hover {
      color: var(--color-ink);
      background: var(--color-surface-2);
    }
    a.active {
      color: var(--color-ink);
      background: var(--color-surface-3);
      border-color: var(--color-ink-3);
    }
  `,
})
export class InsightsNav {
  protected readonly sections = INSIGHTS_SECTIONS;
}
