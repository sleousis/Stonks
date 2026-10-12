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
  { path: '/lab/ledger', label: 'Lab history', exact: false },
  { path: '/lab/sweeps', label: 'Sweep', exact: false, more: true },
  { path: '/lab/signal-ic', label: 'Signal check', exact: false, more: true },
  { path: '/lab/factors', label: 'Factors', exact: false, more: true },
  { path: '/lab/research', label: 'Research sessions', exact: false, more: true },
];

/**
 * Underline tabs for the Lab's screens: the two everyday ones, then "More
 * tools". One row that scrolls sideways on phones, like the page tabs.
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
    // A grid or flex item that never widens the page: the row scrolls instead.
    :host {
      display: block;
      min-width: 0;
      max-width: 100%;
    }
    .lab-nav {
      margin-bottom: var(--space-4);
      border-bottom: 1px solid var(--rule);
    }
    /* One row like the page tabs: it scrolls sideways, and a fading right
       edge hints at tabs past the edge. */
    ul {
      display: flex;
      align-items: stretch;
      gap: 0 var(--space-1);
      margin: 0;
      padding: 0;
      list-style: none;
      white-space: nowrap;
      overflow-x: auto;
      scrollbar-width: none;
      mask-image: linear-gradient(to right, #000 calc(100% - 24px), transparent);
    }
    ul::-webkit-scrollbar {
      display: none;
    }
    li {
      display: flex;
      flex: none;
    }
    li:last-child {
      margin-right: 24px;
    }
    a {
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      padding: 0 var(--space-3);
      border-bottom: 2px solid transparent;
      margin-bottom: -1px;
      color: var(--ink-2);
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
      text-decoration: none;
      transition:
        color var(--dur-fast) var(--ease-out),
        background-color var(--dur-fast) var(--ease-out);
      @include bp.phone {
        min-height: var(--touch-min);
        padding: 0 var(--space-2);
      }
      @include bp.coarse {
        min-height: var(--touch-min);
      }
    }
    a:hover {
      background: var(--sunk);
      color: var(--ink);
    }
    a.active {
      color: var(--ink);
      font-weight: var(--weight-semibold);
      border-bottom-color: var(--accent);
    }
    a:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: -2px;
    }
    /* "More tools": an overline set off by a hairline, inside the row. */
    .more-label {
      align-items: center;
      margin-left: var(--space-2);
      padding: 0 var(--space-2) 0 var(--space-3);
      border-left: 1px solid var(--rule);
      color: var(--muted);
      font-size: var(--text-xs);
      font-weight: 600;
      letter-spacing: var(--tracking-overline);
      text-transform: uppercase;
    }
    @media (prefers-reduced-motion: reduce) {
      a {
        transition: none;
      }
    }
  `,
})
export class LabNav {
  protected readonly main = LAB_SECTIONS.filter((s) => !s.more);
  protected readonly more = LAB_SECTIONS.filter((s) => s.more);
}
