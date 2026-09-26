import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { PageHeader } from '../../shared/ui/page-header';

/**
 * Orders and the ticks that produce them. The header and view tabs stay put;
 * the child routes (orders, fills, ticks, ticks/:id) render below.
 */
@Component({
  selector: 'app-orders-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet, RouterLink, RouterLinkActive, PageHeader],
  template: `
    <app-page-header
      title="Orders"
      description="Orders placed by ticks, the fills they received, and the ticks themselves."
    >
      <a actions class="btn btn-primary" routerLink="/orders/ticks">Run tick</a>
    </app-page-header>

    <nav class="tabs" aria-label="Orders views">
      <a
        routerLink="/orders"
        routerLinkActive="active"
        ariaCurrentWhenActive="page"
        [routerLinkActiveOptions]="{ exact: true }"
        >Orders</a
      >
      <a routerLink="/orders/fills" routerLinkActive="active" ariaCurrentWhenActive="page">Fills</a>
      <a routerLink="/orders/ticks" routerLinkActive="active" ariaCurrentWhenActive="page">Ticks</a>
    </nav>

    <router-outlet />
  `,
  styles: `
    @use 'breakpoints' as bp;

    .tabs {
      display: flex;
      gap: var(--space-1);
      margin: calc(-1 * var(--space-2)) 0 var(--space-4);
      border-bottom: 1px solid var(--color-border);
      overflow-x: auto;
      scrollbar-width: none;
    }
    .tabs a {
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
      transition: color var(--dur-fast) var(--ease);
    }
    .tabs a:hover {
      color: var(--color-ink);
    }
    .tabs a.active {
      color: var(--color-ink);
      border-bottom-color: var(--color-brass);
    }
    @include bp.phone {
      .tabs a {
        flex: 1 1 0;
        justify-content: center;
        min-height: var(--touch-min);
      }
    }
    @include bp.coarse {
      .tabs a {
        min-height: var(--touch-min);
      }
    }
  `,
})
export class OrdersPage {}
