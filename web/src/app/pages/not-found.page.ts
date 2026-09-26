import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

import { PageHeader } from '../shared/ui/page-header';

@Component({
  selector: 'app-not-found-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, RouterLink],
  template: `
    <app-page-header title="Page not found" description="There is no page at this address." />
    <a routerLink="/" class="btn">Go to the dashboard</a>
  `,
})
export class NotFoundPage {}
