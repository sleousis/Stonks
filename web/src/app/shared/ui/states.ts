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
      <span class="visually-hidden">{{ label() }}</span>
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
            var(--hover) 40%,
            var(--pressed) 50%,
            var(--hover) 60%,
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
  host: { '[class.compact]': 'compact()', '[class.good]': "tone() === 'good'" },
  template: `
    <div class="empty">
      <span class="mark" aria-hidden="true">
        <svg
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          @switch (icon()) {
            @case ('orders') {
              <path d="M5 3.5h10v13H5z" />
              <path d="M8 7.5h4M8 10.5h4M8 13.5h2" />
            }
            @case ('inbox') {
              <path d="M3 11.5 5 4.5h10l2 7v4H3z" />
              <path d="M3 11.5h4l1 2h4l1-2h4" />
            }
            @case ('check') {
              <circle cx="10" cy="10" r="7" />
              <path d="m6.8 10.2 2.2 2.2 4.2-4.6" />
            }
            @case ('search') {
              <circle cx="8.5" cy="8.5" r="5" />
              <path d="m12.5 12.5 4 4" />
            }
            @case ('plug') {
              <path d="M7.5 3v4M12.5 3v4" />
              <path d="M5 7h10v2.5a5 5 0 0 1-10 0z" />
              <path d="M10 14.5V17" />
            }
            @case ('pause') {
              <circle cx="10" cy="10" r="7" />
              <path d="M8.2 7.2v5.6M11.8 7.2v5.6" />
            }
            @case ('doc') {
              <path d="M5 2.5h6.5L15 6v11.5H5z" />
              <path d="M11.5 2.5V6H15M7.5 10h5M7.5 13h5" />
            }
            @default {
              <path d="M3 14.5 7.5 10l3 3L17 6.5" />
            }
          }
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
    /* Good news (nothing halted, no failures) reads apart from missing data. */
    :host(.good) .mark {
      background: var(--up-soft);
      color: var(--up);
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
  /** The glyph in the squircle: what kind of thing would fill this space. */
  readonly icon = input<EmptyIcon>('chart');
  /** `good` for all-clear states ("Trading is not halted"): tinted in --up. */
  readonly tone = input<'default' | 'good'>('default');
}

export type EmptyIcon =
  'chart' | 'orders' | 'inbox' | 'check' | 'search' | 'plug' | 'pause' | 'doc';

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
