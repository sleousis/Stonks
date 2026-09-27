import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { EmptyState } from './states';

/** Where "Open a paper portfolio" goes: the first-run guide, portfolio step open. */
export const OPEN_PORTFOLIO_LINK = '/welcome';
export const OPEN_PORTFOLIO_QUERY = { step: 'portfolio' } as const;

/**
 * Whether a money page may read the picked portfolio (UX-13):
 * - `pending` while the portfolio list is loading (read nothing yet),
 * - `none` once it has loaded empty (show `<app-no-book>`, read nothing),
 * - `ready` otherwise. A list that failed, a server without the list route,
 *   or a list nobody asked for (`idle`, as in page tests) all read as before.
 *
 * Put it in a resource's params: `bookState(ctx) === 'ready' ? {...} : undefined`.
 */
export function bookState(ctx: PortfolioContextService): 'pending' | 'none' | 'ready' {
  if (ctx.state() === 'loading') return 'pending';
  return ctx.noBook() ? 'none' : 'ready';
}

/**
 * The empty state of every money page for a trader with no portfolio: one
 * action, straight to opening a paper portfolio in the first-run guide.
 */
@Component({
  selector: 'app-no-book',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, EmptyState],
  template: `
    <app-empty-state [title]="title()" [message]="message()">
      <a [routerLink]="link" [queryParams]="query" class="btn btn-primary"
        >Open a paper portfolio</a
      >
    </app-empty-state>
  `,
  styles: `
    :host {
      display: block;
    }
  `,
})
export class NoBook {
  readonly title = input('No portfolio yet');
  readonly message = input(
    'A paper portfolio trades with pretend money, so you can see what a strategy would do. It takes a minute to open one.',
  );
  protected readonly link = OPEN_PORTFOLIO_LINK;
  protected readonly query = OPEN_PORTFOLIO_QUERY;
}
