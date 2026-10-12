import {
  ChangeDetectionStrategy,
  Component,
  booleanAttribute,
  computed,
  input,
  output,
} from '@angular/core';

import { errorMessage } from '../../core/http/api-error';

/**
 * Loading: skeleton rows in the final shape, with a slow shimmer that stops
 * under reduced motion. The label is for screen readers. No spinner: those
 * live only inside a working button.
 *
 *   @if (portfolio.isLoading()) { <app-loading-state label="Loading positions" [rows]="5" /> }
 */
@Component({
  selector: 'app-loading-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="loading" role="status" [attr.aria-label]="label()">
      @for (row of rowList(); track $index) {
        <div class="row" aria-hidden="true">
          <span class="bar" [style.width.%]="row"></span>
          <span class="bar short"></span>
        </div>
      }
    </div>
  `,
  styles: `
    /* Loading, empty and error all reserve 9.5rem, so swapping one for
       another does not push the page around. Skeleton rows in the final
       shape: a line and a short figure, a hairline between. */
    .loading {
      display: grid;
      align-content: start;
      min-height: 9.5rem;
    }
    .row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-6);
      height: var(--row-h);
      border-bottom: 1px solid var(--rule);
    }
    /* A slow, subtle shimmer: stops under reduced motion. */
    .bar {
      height: 10px;
      max-width: 60%;
      border-radius: var(--r-tag);
      background:
        linear-gradient(
            90deg,
            transparent 0%,
            var(--color-hover) 40%,
            var(--color-pressed) 50%,
            var(--color-hover) 60%,
            transparent 100%
          )
          0 0 / 200% 100% no-repeat,
        var(--sunk);
      animation: shimmer 2s var(--ease-out) infinite;
    }
    .bar.short {
      flex: none;
      width: 4.5rem;
    }
    @keyframes shimmer {
      from {
        background-position:
          150% 0,
          0 0;
      }
      to {
        background-position:
          -50% 0,
          0 0;
      }
    }
    @media (prefers-reduced-motion: reduce) {
      .bar {
        animation: none;
      }
    }
  `,
})
export class LoadingState {
  readonly label = input('Loading');
  readonly rows = input(3);
  protected readonly rowList = computed(() =>
    Array.from({ length: this.rows() }, (_, i) => [92, 76, 84, 64, 88][i % 5]),
  );
}

/**
 * Nothing to show yet: left-aligned, a 40px sunk squircle with an icon,
 * what would fill this space, and one clear action in the content slot (a
 * button or a link). Pass `compact`
 * when the empty state is a panel's only content: a lower floor.
 */
@Component({
  selector: 'app-empty-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[class.compact]': 'compact()' },
  template: `
    <div class="empty">
      <span class="mark" aria-hidden="true">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6">
          <path d="M3 14.5 7.5 10l3 3L17 6.5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </span>
      <div class="body">
        <p class="title">{{ title() }}</p>
        @if (message()) {
          <p class="message">{{ message() }}</p>
        }
        <div class="action"><ng-content /></div>
      </div>
    </div>
  `,
  styles: `
    .empty {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      align-items: start;
      gap: var(--space-3) var(--space-4);
      /* Same floor as the loading and error states. */
      min-height: 9.5rem;
      padding: var(--space-6) 0;
    }
    :host(.compact) .empty {
      min-height: 7rem;
      padding: var(--space-4) 0;
    }
    /* A 40px sunk squircle with a 20px icon. */
    .mark {
      display: grid;
      place-items: center;
      width: 40px;
      height: 40px;
      border-radius: var(--r-sheet);
      background: var(--sunk);
      color: var(--muted);
    }
    .mark svg {
      width: 20px;
      height: 20px;
    }
    .body {
      display: grid;
      justify-items: start;
      gap: var(--space-1);
      min-width: 0;
    }
    .title {
      font-size: var(--text-lead);
      font-weight: var(--weight-semibold);
      line-height: var(--leading-tight);
    }
    .message {
      color: var(--ink-2);
      max-width: 68ch;
    }
    .action {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      margin-top: var(--space-2);
    }
    .action:empty {
      display: none;
    }
  `,
})
export class EmptyState {
  readonly title = input.required<string>();
  readonly message = input<string | null>(null);
  /** The panel's only content: a 7rem floor instead of 9.5rem. */
  readonly compact = input(false, { transform: booleanAttribute });
}

/**
 * A failed load: the error band with the API's plain message and a retry.
 *
 *   @if (res.error(); as err) { <app-error-state [error]="err" (retry)="res.reload()" /> }
 */
@Component({
  selector: 'app-error-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="error" role="alert">
      <div class="body">
        <p class="title">{{ title() }}</p>
        <p class="message">{{ message() }}</p>
        <button type="button" class="btn" (click)="retry.emit()">Try again</button>
      </div>
    </div>
  `,
  styles: `
    /* The error band: --down-soft with a 3px red left edge. */
    .error {
      display: grid;
      align-items: start;
      min-height: calc(9.5rem - 2 * var(--space-3));
      margin: var(--space-3) 0;
      padding: var(--space-4);
      border-left: 3px solid var(--down);
      border-radius: var(--r-sheet);
      background: var(--down-soft);
    }
    .body {
      display: grid;
      justify-items: start;
      gap: var(--space-2);
      min-width: 0;
    }
    .title {
      color: var(--ink);
      font-weight: var(--weight-semibold);
    }
    .message {
      max-width: 68ch;
      color: var(--ink-2);
      overflow-wrap: anywhere;
    }
  `,
})
export class ErrorState {
  readonly error = input.required<unknown>();
  readonly title = input('Could not load this');
  readonly retry = output<void>();
  protected readonly message = computed(() => errorMessage(this.error()));
}
