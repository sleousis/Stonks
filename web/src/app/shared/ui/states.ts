import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { errorMessage } from '../../core/http/api-error';

/**
 * Loading placeholder: skeleton rows sized like the content they stand for.
 *
 *   @if (portfolio.isLoading()) { <app-loading-state label="Loading positions" [rows]="5" /> }
 */
@Component({
  selector: 'app-loading-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="loading" role="status" [attr.aria-label]="label()">
      @for (row of rowList(); track $index) {
        <div class="bar" [style.width.%]="row"></div>
      }
    </div>
  `,
  styles: `
    /* Loading, empty and error all reserve 9.5rem (an error with a one-line
       message and a 44px retry button), so swapping one for another does
       not push the page around. Pass more rows when the content is taller. */
    .loading {
      display: grid;
      align-content: start;
      gap: var(--space-2);
      min-height: 9.5rem;
      padding: var(--space-4);
    }
    .bar {
      height: 14px;
      border-radius: var(--radius-sm);
      background: linear-gradient(
        90deg,
        var(--color-surface-3),
        var(--color-surface-2),
        var(--color-surface-3)
      );
      background-size: 200% 100%;
      animation: shimmer 1.4s linear infinite;
    }
    @keyframes shimmer {
      to {
        background-position: -200% 0;
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
 * Nothing to show yet. Say what would fill this space and how to get there;
 * put the action button in the content slot.
 */
@Component({
  selector: 'app-empty-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="empty">
      <p class="title">{{ title() }}</p>
      @if (message()) {
        <p class="message">{{ message() }}</p>
      }
      <ng-content />
    </div>
  `,
  styles: `
    .empty {
      display: grid;
      justify-items: start;
      align-content: start;
      gap: var(--space-2);
      /* Same floor as the loading and error states. */
      min-height: 9.5rem;
      padding: var(--space-5) var(--space-4);
    }
    .title {
      font-weight: var(--weight-semibold);
    }
    .message {
      color: var(--color-ink-2);
      max-width: 60ch;
    }
  `,
})
export class EmptyState {
  readonly title = input.required<string>();
  readonly message = input<string | null>(null);
}

/**
 * A failed load, with the API's message and a retry button.
 *
 *   @if (res.error(); as err) { <app-error-state [error]="err" (retry)="res.reload()" /> }
 */
@Component({
  selector: 'app-error-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="error" role="alert">
      <p class="title">{{ title() }}</p>
      <p class="message">{{ message() }}</p>
      <button type="button" class="btn" (click)="retry.emit()">Try again</button>
    </div>
  `,
  styles: `
    .error {
      display: grid;
      justify-items: start;
      align-content: start;
      gap: var(--space-2);
      /* 9.5rem with its margins: the loading and empty states' floor. */
      min-height: calc(9.5rem - 2 * var(--space-3));
      margin: var(--space-3);
      padding: var(--space-3) var(--space-4);
      border-left: 3px solid var(--color-loss);
      background: var(--color-loss-soft);
      border-radius: var(--radius-sm);
    }
    .title {
      font-weight: var(--weight-semibold);
      color: var(--color-loss);
    }
    .message {
      color: var(--color-ink);
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
