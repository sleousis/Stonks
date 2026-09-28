import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

import { PageHeader } from '../shared/ui/page-header';

/**
 * The one page anyone sees when they open a page their role may not use
 * (m10). The address stays as it was, so a shared link still reads right.
 */
@Component({
  selector: 'app-no-access-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, RouterLink],
  template: `
    <app-page-header
      title="No access"
      description="This page is for admins. Ask your admin if you need what it shows."
    />
    <a routerLink="/" class="btn">Go to Today</a>
  `,
})
export class NoAccessPage {}
