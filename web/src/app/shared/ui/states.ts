import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { errorMessage } from '../../core/http/api-error';
import { BrandMark } from './brand-mark';

/**
 * Loading: the brand mark draws itself beside the label, over faint rows
 * sized like the content they stand for.
 *
 *   @if (portfolio.isLoading()) { <app-loading-state label="Loading positions" [rows]="5" /> }
 */
@Component({
  selector: 'app-loading-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BrandMark],
  template: `
    <div class="loading" role="status" [attr.aria-label]="label()">
      <p class="head" aria-hidden="true">
        <app-brand-mark mode="loading" [size]="22" />
        <span>{{ label() }}</span>
      </p>
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
    .head {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      margin-bottom: var(--space-1);
      font-size: var(--text-sm);
      color: var(--color-ink-3);
    }
    .bar {
      height: 10px;
      border-radius: var(--radius-xs);
      background: var(--color-surface-3);
      opacity: 0.7;
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
 * Nothing to show yet: the still brand mark, what would fill this space, and
 * one clear action in the content slot (a button or a link).
 */
@Component({
  selector: 'app-empty-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BrandMark],
  template: `
    <div class="empty">
      <app-brand-mark class="mark" [size]="28" />
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
      padding: var(--space-5) var(--space-4);
    }
    .mark {
      padding: var(--space-2);
      border: 1px dashed var(--color-border-strong);
      border-radius: var(--radius-sm);
      color: var(--color-paper);
    }
    .body {
      display: grid;
      justify-items: start;
      gap: var(--space-1);
      min-width: 0;
    }
    .title {
      font-family: var(--font-display);
      font-stretch: var(--display-stretch);
      font-size: var(--text-xl);
      font-weight: var(--weight-bold);
      letter-spacing: var(--tracking-display);
      line-height: var(--leading-tight);
    }
    .message {
      color: var(--color-ink-2);
      max-width: 60ch;
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
}

/**
 * A failed load: the brand mark turning down, the API's message and a retry.
 *
 *   @if (res.error(); as err) { <app-error-state [error]="err" (retry)="res.reload()" /> }
 */
@Component({
  selector: 'app-error-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BrandMark],
  template: `
    <div class="error" role="alert">
      <app-brand-mark mode="broken" [size]="28" />
      <div class="body">
        <p class="title">{{ title() }}</p>
        <p class="message">{{ message() }}</p>
        <button type="button" class="btn" (click)="retry.emit()">Try again</button>
      </div>
    </div>
  `,
  styles: `
    .error {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      align-items: start;
      gap: var(--space-3);
      /* 9.5rem with its margins: the loading and empty states' floor. */
      min-height: calc(9.5rem - 2 * var(--space-3));
      margin: var(--space-3);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-loss);
      border-left-width: 4px;
      background: var(--color-loss-soft);
      border-radius: var(--radius-sm);
    }
    .body {
      display: grid;
      justify-items: start;
      gap: var(--space-2);
      min-width: 0;
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
