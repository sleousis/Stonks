import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

import { PageHeader } from '../shared/ui/page-header';
import { EmptyState } from '../shared/ui/states';

/** Any address the console does not know: what happened and one way back. */
@Component({
  selector: 'app-not-found-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, RouterLink, EmptyState],
  template: `
    <app-page-header title="Page not found" />
    <app-empty-state
      compact
      title="There is no page at this address."
      message="Check the address, or start again from Today."
    >
      <a routerLink="/" class="btn btn-primary">Go home</a>
    </app-empty-state>
  `,
  styles: `
    /* Centred in the content column, with no panel round it. */
    :host {
      display: block;
      max-width: 36rem;
      margin: var(--space-7) auto 0;
    }
    /* The empty state's own padding lines its mark up with the title. */
    app-empty-state {
      display: block;
      margin-inline: calc(-1 * var(--space-4));
    }
  `,
})
export class NotFoundPage {}
