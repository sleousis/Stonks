import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

import { PageHeader } from '../shared/ui/page-header';
import { EmptyState } from '../shared/ui/states';

/**
 * The one page anyone sees when they open a page their role may not use
 * (m10). The address stays as it was, so a shared link still reads right.
 */
@Component({
  selector: 'app-no-access-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, RouterLink, EmptyState],
  template: `
    <app-page-header title="No access" />
    <app-empty-state
      compact
      title="This page is for admins."
      message="Ask your admin if you need what it shows."
    >
      <a routerLink="/" class="btn btn-primary">Go to Today</a>
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
export class NoAccessPage {}
