import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterRenderEffect,
  inject,
  viewChild,
} from '@angular/core';

import { ToastService } from '../../core/notify/toast.service';

/**
 * Renders ToastService notifications. Mounted once, in the shell.
 *
 * - The container is a manual popover, so it sits in the browser's top
 *   layer: a toast raised while a modal dialog is open shows above it and
 *   stays clickable. It is raised again whenever a toast arrives.
 * - No `aria-live` on the container: each toast is its own live region
 *   (`role="alert"` for errors, `role="status"` otherwise), so nothing is
 *   read twice.
 * - Hover or focus pauses a toast's countdown; errors stay until dismissed.
 */
@Component({
  selector: 'app-toast-outlet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section #layer class="toasts" popover="manual" aria-label="Notifications">
      @for (t of toasts.toasts(); track t.id) {
        <div
          class="toast"
          [attr.data-tone]="t.tone"
          [attr.role]="t.tone === 'error' ? 'alert' : 'status'"
          (mouseenter)="toasts.pause(t.id, 'hover')"
          (mouseleave)="toasts.resume(t.id, 'hover')"
          (focusin)="toasts.pause(t.id, 'focus')"
          (focusout)="focusLeft($event, t.id)"
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
      /* Undo the popover defaults (centred box with a border). */
      inset: auto;
      margin: 0;
      padding: 0;
      border: 0;
      background: transparent;
      color: inherit;
      overflow: visible;
      position: fixed;
      z-index: 50;
      right: var(--space-4);
      bottom: calc(var(--space-4) + env(safe-area-inset-bottom));
      display: grid;
      gap: var(--space-2);
      width: min(380px, calc(100vw - 32px));
      pointer-events: none;
    }
    /* Browsers without popover support drop this rule (unknown selector)
       and show the layer as a plain fixed box. */
    .toasts:not(:popover-open) {
      display: none;
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
  private readonly layer = viewChild.required<ElementRef<HTMLElement>>('layer');
  private shown = false;
  private count = 0;

  constructor() {
    afterRenderEffect(() => {
      const count = this.toasts.toasts().length;
      const el = this.layer().nativeElement;
      const grew = count > this.count;
      this.count = count;
      if (typeof el.showPopover !== 'function') return;
      if (count === 0) {
        if (this.shown) el.hidePopover();
        this.shown = false;
        return;
      }
      // Re-open to move above a dialog opened since, unless the trader is
      // inside a toast (hiding would drop their focus).
      if (this.shown && grew && !el.contains(document.activeElement)) {
        el.hidePopover();
        this.shown = false;
      }
      if (!this.shown) {
        el.showPopover();
        this.shown = true;
      }
    });
  }

  protected focusLeft(event: FocusEvent, id: number): void {
    const toast = event.currentTarget as HTMLElement;
    if (toast.contains(event.relatedTarget as Node | null)) return;
    this.toasts.resume(id, 'focus');
  }
}
