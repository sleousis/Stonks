import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { PageHeader } from './page-header';

/**
 * Temporary body for pages not built yet: what the page will do and which
 * API routes it will use. Delete it from a page when you build that page.
 */
@Component({
  selector: 'app-planned-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader],
  template: `
    <app-page-header [title]="title()" [description]="description()" />
    <section class="panel planned" aria-labelledby="planned-title">
      <h2 id="planned-title">Coming next</h2>
      <ul>
        @for (item of plans(); track item) {
          <li>{{ item }}</li>
        }
      </ul>
      @if (routes().length) {
        <p class="routes-label">API routes</p>
        <ul class="routes">
          @for (route of routes(); track route) {
            <li>
              <code>{{ route }}</code>
            </li>
          }
        </ul>
      }
    </section>
  `,
  styles: `
    .planned {
      display: grid;
      gap: var(--space-3);
      max-width: 72ch;
      padding: var(--space-4) var(--space-5);
    }
    h2 {
      font-size: var(--text-lg);
    }
    ul {
      margin: 0;
      padding-left: 1.2em;
      color: var(--color-ink-2);
    }
    li + li {
      margin-top: var(--space-1);
    }
    .routes-label {
      font-size: var(--text-sm);
      font-weight: var(--weight-semibold);
    }
    code {
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
  `,
})
export class PlannedPage {
  readonly title = input.required<string>();
  readonly description = input.required<string>();
  readonly plans = input.required<readonly string[]>();
  readonly routes = input<readonly string[]>([]);
}
