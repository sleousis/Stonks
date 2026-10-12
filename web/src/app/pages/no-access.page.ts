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
    <app-page-header
      title="No access"
      description="Your role cannot open this page. The address stays as it was."
    />
    <section class="page-section">
      <app-empty-state
        title="This page is for admins."
        message="Ask your admin if you need what it shows."
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
export class NoAccessPage {}
