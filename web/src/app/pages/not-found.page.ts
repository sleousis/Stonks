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
    <app-page-header
      title="Page not found"
      description="There is no page at this address. It may have moved, or the link has a typo."
    />
    <section class="page-section">
      <app-empty-state
        title="Start again from Today"
        message="Today holds your portfolio, the strategies you follow and what runs next."
      >
        <a routerLink="/" class="btn btn-primary">Go to Today</a>
      </app-empty-state>
    </section>
  `,
  styles: `
    :host {
      display: block;
      max-width: 44rem;
    }
  `,
})
export class NotFoundPage {}
