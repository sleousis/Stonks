import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/**
 * Page title row. Every page starts with one.
 *
 *   <app-page-header title="Strategies" description="Registered strategies and their status.">
 *     <button actions class="btn">Refresh</button>
 *   </app-page-header>
 */
@Component({
  selector: 'app-page-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <header class="page-header">
      <div class="text">
        <h1 tabindex="-1">{{ title() }}</h1>
        @if (description()) {
          <p class="description">{{ description() }}</p>
        }
        <ng-content />
      </div>
      <div class="actions"><ng-content select="[actions]" /></div>
    </header>
  `,
  styles: `
    .page-header {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      justify-content: space-between;
      gap: var(--space-3) var(--space-4);
      margin-bottom: var(--space-5);
    }
    .text {
      min-width: 0;
      flex: 1 1 18rem;
    }
    h1 {
      font-size: var(--text-xl);
      letter-spacing: -0.01em;
      outline: none;
    }
    .description {
      margin-top: var(--space-1);
      color: var(--color-ink-2);
      max-width: 70ch;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .actions:empty {
      display: none;
    }
  `,
})
export class PageHeader {
  readonly title = input.required<string>();
  readonly description = input<string | null>(null);
}
