import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

/** The Lab's screens, as links so each has its own address. */
export const LAB_SECTIONS = [
  { path: '/lab', label: 'Backtest and lab run', exact: true },
  { path: '/lab/sweeps', label: 'Sweep', exact: false },
  { path: '/lab/signal-ic', label: 'Signal IC', exact: false },
  { path: '/lab/ledger', label: 'Trial ledger', exact: false },
] as const;

@Component({
  selector: 'app-lab-nav',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav class="lab-nav" aria-label="Lab screens">
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
    .lab-nav {
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
export class LabNav {
  protected readonly sections = LAB_SECTIONS;
}
