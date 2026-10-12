import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

/**
 * Page title row. Every page starts with one: the H1 (34px, 26 on phones),
 * the actions on the right on the H1's baseline, and under it one
 * standfirst sentence (15px, 20 words at most), built from data when it can
 * be ("3 approved, 2 on trial. Quant value leads at +8.42%."). No eyebrow,
 * no breadcrumbs. A sub-page gets a small back link above the H1.
 * Actions: one primary, then secondary, then ghost. On phones they sit
 * under the standfirst, the primary at full width and the rest in one row
 * (the rules live in styles/_controls.scss, since the actions are projected).
 *
 *   <app-page-header title="Strategies" [description]="standfirst()">
 *     <a actions class="btn btn-primary" routerLink="/studio">New strategy</a>
 *   </app-page-header>
 *
 *   <app-page-header title="Momentum 12-1" backPath="/strategies" backLabel="Strategies" />
 */
@Component({
  selector: 'app-page-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    <header class="page-header">
      <div class="text">
        @if (backPath()) {
          <a class="back" [routerLink]="backPath()">
            <svg
              viewBox="0 0 12 12"
              fill="none"
              stroke="currentColor"
              stroke-width="1.6"
              aria-hidden="true"
            >
              <path d="M7.5 3 4.5 6l3 3" />
            </svg>
            {{ backLabel() ?? 'Back' }}
          </a>
        }
        <h1 tabindex="-1">{{ title() }}</h1>
        @if (description()) {
          <p class="standfirst description">{{ description() }}</p>
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
      gap: var(--space-4) var(--space-6);
      /* 28px to the first section. */
      margin-bottom: 28px;
    }
    .text {
      min-width: 0;
      flex: 1 1 22rem;
    }
    .back {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      min-height: 24px;
      margin-bottom: var(--space-1);
      color: var(--muted);
      font-size: 0.8125rem;
      font-weight: var(--weight-label);
      text-decoration: none;
    }
    .back svg {
      width: 12px;
      height: 12px;
    }
    .back:hover {
      color: var(--ink);
    }
    h1 {
      font-size: var(--text-title);
      font-weight: var(--weight-title);
      letter-spacing: var(--tracking-title);
      line-height: 1.12;
      outline: none;
      overflow-wrap: anywhere;
    }
    .description {
      margin-top: var(--space-2);
      overflow-wrap: anywhere;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
    .actions:empty {
      display: none;
    }
    @include bp.phone {
      .page-header {
        gap: var(--space-4);
        margin-bottom: var(--space-6);
      }
      .back {
        min-height: var(--touch-min);
        margin-bottom: 0;
      }
      /* A two-column grid: the primary across the top, the rest in one row. */
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
  /** A sub-page: the parent page's path, shown as a back link above the H1. */
  readonly backPath = input<string | null>(null);
  /** The back link's words (the parent page's name). */
  readonly backLabel = input<string | null>(null);
}
