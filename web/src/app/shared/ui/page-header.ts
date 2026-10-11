import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/**
 * Page title row. Every page starts with one. On phones the actions form a
 * two-column grid: a primary button takes the whole row, the others share
 * rows (the rules sit in styles.scss, since the actions are projected).
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
          <p class="description prose">{{ description() }}</p>
        }
        <ng-content />
      </div>
      <div class="actions"><ng-content select="[actions]" /></div>
    </header>
  `,
  styles: `
    @use 'breakpoints' as bp;

    .page-header {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      justify-content: space-between;
      gap: var(--space-3) var(--space-4);
      /* 20px to the content below. */
      margin-bottom: calc(var(--space-4) + var(--space-1));
    }
    .text {
      min-width: 0;
      flex: 1 1 18rem;
    }
    h1 {
      font-family: var(--font-display);
      font-stretch: var(--display-stretch);
      font-size: var(--text-title);
      font-weight: var(--weight-bold);
      letter-spacing: var(--tracking-display);
      line-height: var(--leading-tight);
      outline: none;
      overflow-wrap: anywhere;
    }
    .description {
      margin-top: var(--space-1);
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      line-height: var(--leading-body);
      max-width: 70ch;
      overflow-wrap: anywhere;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .actions:empty {
      display: none;
    }
    /* Phones: a two-column grid instead of ragged wrapping. */
    @include bp.phone {
      .actions {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        width: 100%;
      }
    }
  `,
})
export class PageHeader {
  readonly title = input.required<string>();
  readonly description = input<string | null>(null);
}
