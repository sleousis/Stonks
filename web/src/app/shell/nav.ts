import { ChangeDetectionStrategy, Component, output } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

import { NAV_GROUPS, NAV_ITEMS } from './nav-items';

@Component({
  selector: 'app-nav',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav aria-label="Main">
      @for (group of groups; track group) {
        <p class="group" [id]="'nav-group-' + group">{{ group }}</p>
        <ul [attr.aria-labelledby]="'nav-group-' + group">
          @for (item of itemsIn(group); track item.path) {
            <li>
              <a
                [routerLink]="item.path"
                routerLinkActive="active"
                ariaCurrentWhenActive="page"
                [routerLinkActiveOptions]="{ exact: item.path === '/' }"
                [attr.aria-keyshortcuts]="'g ' + item.key"
                (click)="navigate.emit()"
              >
                <span>{{ item.label }}</span>
                <kbd aria-hidden="true">g {{ item.key }}</kbd>
              </a>
            </li>
          }
        </ul>
      }
    </nav>
  `,
  styles: `
    nav {
      display: grid;
      gap: var(--space-1);
    }
    .group {
      margin: var(--space-3) 0 var(--space-1);
      padding: 0 var(--space-3);
      font-size: var(--text-xs);
      font-weight: var(--weight-semibold);
      color: var(--color-ink-3);
    }
    ul {
      display: grid;
      gap: 2px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    a {
      position: relative;
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 34px;
      padding: 0 var(--space-3);
      border-radius: var(--radius-sm);
      color: var(--color-ink-2);
      text-decoration: none;
      font-weight: var(--weight-medium);
    }
    a:hover {
      background: var(--color-surface-2);
      color: var(--color-ink);
    }
    a.active {
      background: var(--color-surface-3);
      color: var(--color-ink);
      font-weight: var(--weight-semibold);
    }
    a.active::before {
      content: '';
      position: absolute;
      left: 0;
      top: 7px;
      bottom: 7px;
      width: 3px;
      border-radius: 2px;
      background: var(--color-brass);
    }
    kbd {
      font: inherit;
      font-size: var(--text-xs);
      color: var(--color-ink-3);
      opacity: 0;
    }
    a:hover kbd,
    a:focus-visible kbd {
      opacity: 1;
    }
    @media (pointer: coarse), (max-width: 767.98px) {
      a {
        min-height: var(--touch-min);
      }
      kbd {
        display: none;
      }
    }
  `,
})
export class Nav {
  readonly navigate = output<void>();
  protected readonly groups = NAV_GROUPS;
  protected itemsIn(group: string) {
    return NAV_ITEMS.filter((i) => i.group === group);
  }
}
