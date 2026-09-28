import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

export interface LabSection {
  path: string;
  label: string;
  exact: boolean;
  /** Research tools most traders never need sit under "More tools". */
  more?: boolean;
}

/** The Lab's screens, as links so each has its own address. */
export const LAB_SECTIONS: readonly LabSection[] = [
  { path: '/lab', label: 'Test a strategy', exact: true },
  { path: '/lab/ledger', label: 'Trial ledger', exact: false },
  { path: '/lab/sweeps', label: 'Sweep', exact: false, more: true },
  { path: '/lab/signal-ic', label: 'Signal IC', exact: false, more: true },
  { path: '/lab/factors', label: 'Factors', exact: false, more: true },
  { path: '/lab/research', label: 'Research sessions', exact: false, more: true },
];

/**
 * Underline tabs for the Lab's screens: the two everyday ones, then "More
 * tools". One row that scrolls sideways on phones, never two rows of pills.
 */
@Component({
  selector: 'app-lab-nav',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav class="lab-nav" aria-label="Lab screens">
      <ul>
        @for (s of main; track s.path) {
          <li>
            <a
              [routerLink]="s.path"
              routerLinkActive="active"
              ariaCurrentWhenActive="page"
              [routerLinkActiveOptions]="{ exact: s.exact }"
              >{{ s.label }}</a
            >
          </li>
        }
        <li class="more-label" aria-hidden="true">More tools</li>
        @for (s of more; track s.path) {
          <li>
            <a
              [routerLink]="s.path"
              routerLinkActive="active"
              ariaCurrentWhenActive="page"
              [routerLinkActiveOptions]="{ exact: s.exact }"
              [attr.aria-label]="s.label + ', more tools'"
              >{{ s.label }}</a
            >
          </li>
        }
      </ul>
    </nav>
  `,
  styles: `
    @use 'breakpoints' as bp;
    .lab-nav {
      margin-bottom: var(--space-4);
      border-bottom: 1px solid var(--color-border);
      overflow-x: auto;
      scrollbar-width: none;
      @include bp.phone {
        mask-image: linear-gradient(to right, #000 85%, transparent);
      }
    }
    .lab-nav::-webkit-scrollbar {
      display: none;
    }
    ul {
      display: flex;
      align-items: stretch;
      gap: var(--space-1);
      margin: 0;
      padding: 0;
      list-style: none;
      white-space: nowrap;
    }
    li {
      display: flex;
    }
    a {
      display: inline-flex;
      align-items: center;
      min-height: 2.5rem;
      padding: 0 var(--space-3);
      border-bottom: 2px solid transparent;
      margin-bottom: -1px;
      color: var(--color-ink-2);
      text-decoration: none;
      font-weight: var(--weight-medium);
      @include bp.phone {
        min-height: var(--touch-min);
      }
      @include bp.coarse {
        min-height: var(--touch-min);
      }
    }
    a:hover {
      color: var(--color-ink);
    }
    a.active {
      color: var(--color-ink);
      border-bottom-color: var(--color-accent);
    }
    a:focus-visible {
      outline: 2px solid var(--color-focus);
      outline-offset: -2px;
    }
    .more-label {
      align-items: center;
      margin-left: var(--space-3);
      padding-left: var(--space-3);
      border-left: 1px solid var(--color-border);
      font-size: var(--text-xs);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--color-ink-3);
    }
  `,
})
export class LabNav {
  protected readonly main = LAB_SECTIONS.filter((s) => !s.more);
  protected readonly more = LAB_SECTIONS.filter((s) => s.more);
}
