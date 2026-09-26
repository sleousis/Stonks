import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { ToastService } from '../../core/notify/toast.service';

/** Renders ToastService notifications. Mounted once, in the shell. */
@Component({
  selector: 'app-toast-outlet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="toasts" aria-label="Notifications" aria-live="polite">
      @for (t of toasts.toasts(); track t.id) {
        <div
          class="toast"
          [attr.data-tone]="t.tone"
          [attr.role]="t.tone === 'error' ? 'alert' : 'status'"
        >
          <div class="body">
            @if (t.title) {
              <p class="title">{{ t.title }}</p>
            }
            <p class="message">{{ t.message }}</p>
          </div>
          <button
            type="button"
            class="btn btn-ghost btn-icon close"
            aria-label="Dismiss notification"
            (click)="toasts.dismiss(t.id)"
          >
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
              <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" />
            </svg>
          </button>
        </div>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;

    .toasts {
      position: fixed;
      z-index: 50;
      right: var(--space-4);
      bottom: calc(var(--space-4) + env(safe-area-inset-bottom));
      display: grid;
      gap: var(--space-2);
      width: min(380px, calc(100vw - 32px));
      pointer-events: none;
    }
    @include bp.phone {
      .toasts {
        left: var(--space-4);
        right: var(--space-4);
        width: auto;
      }
    }
    .toast {
      display: flex;
      align-items: flex-start;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-2) var(--space-3) var(--space-4);
      border: 1px solid var(--color-border);
      border-left: 3px solid var(--color-info);
      border-radius: var(--radius-md);
      background: var(--color-surface);
      box-shadow: var(--shadow-2);
      pointer-events: auto;
      animation: rise var(--dur) var(--ease);
    }
    .toast[data-tone='success'] {
      border-left-color: var(--color-gain);
    }
    .toast[data-tone='error'] {
      border-left-color: var(--color-loss);
    }
    .body {
      flex: 1;
      min-width: 0;
    }
    .title {
      font-weight: var(--weight-semibold);
    }
    .message {
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .close {
      flex: none;
    }
    @keyframes rise {
      from {
        opacity: 0;
        transform: translateY(6px);
      }
    }
  `,
})
export class ToastOutlet {
  protected readonly toasts = inject(ToastService);
}
