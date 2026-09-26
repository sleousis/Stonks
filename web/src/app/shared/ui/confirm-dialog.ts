import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  computed,
  effect,
  inject,
  linkedSignal,
  viewChild,
} from '@angular/core';

import { ConfirmService } from '../../core/confirm/confirm.service';

/**
 * Renders ConfirmService requests in a modal <dialog>. Mounted once, in the
 * shell. On phones it becomes a full-screen sheet.
 */
@Component({
  selector: 'app-confirm-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <dialog
      #dialog
      class="sheet"
      aria-labelledby="confirm-title"
      aria-describedby="confirm-message"
      (cancel)="$event.preventDefault(); answer(false)"
    >
      @if (request(); as req) {
        <form method="dialog" (submit)="$event.preventDefault(); answer(true)">
          <h2 id="confirm-title">{{ req.title }}</h2>
          <p id="confirm-message" class="message">{{ req.message }}</p>
          @if (req.typedConfirmation) {
            <div class="field">
              <label for="confirm-typed">
                Type <strong class="phrase">{{ req.typedConfirmation }}</strong> to confirm
              </label>
              <input
                id="confirm-typed"
                class="input"
                autocomplete="off"
                autocapitalize="off"
                spellcheck="false"
                [value]="typed()"
                (input)="typed.set($any($event.target).value)"
              />
            </div>
          }
          <div class="actions">
            <button type="button" class="btn" (click)="answer(false)">
              {{ req.cancelLabel ?? 'Cancel' }}
            </button>
            <button
              type="submit"
              class="btn"
              [class.btn-danger]="req.tone === 'danger'"
              [class.btn-primary]="req.tone !== 'danger'"
              [disabled]="!canConfirm()"
            >
              {{ req.confirmLabel }}
            </button>
          </div>
        </form>
      }
    </dialog>
  `,
  styles: `
    @use 'breakpoints' as bp;

    .sheet {
      width: min(460px, calc(100vw - 32px));
      padding: 0;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-lg);
      background: var(--color-surface);
      color: var(--color-ink);
      box-shadow: var(--shadow-2);
    }
    .sheet::backdrop {
      background: var(--color-scrim);
    }
    form {
      display: grid;
      gap: var(--space-4);
      padding: var(--space-5);
    }
    h2 {
      font-size: var(--text-lg);
    }
    .message {
      color: var(--color-ink-2);
    }
    .phrase {
      font-weight: var(--weight-bold);
      user-select: all;
    }
    .actions {
      display: flex;
      justify-content: flex-end;
      gap: var(--space-2);
    }
    @include bp.phone {
      .sheet {
        width: 100vw;
        max-width: 100vw;
        height: 100dvh;
        max-height: 100dvh;
        margin: 0;
        border: 0;
        border-radius: 0;
      }
      form {
        min-height: 100%;
        align-content: start;
        padding: calc(var(--space-5) + env(safe-area-inset-top)) var(--space-4)
          calc(var(--space-4) + env(safe-area-inset-bottom));
      }
      .actions {
        margin-top: auto;
        flex-direction: column-reverse;
      }
      .actions .btn {
        width: 100%;
      }
    }
  `,
})
export class ConfirmDialog {
  private readonly confirm = inject(ConfirmService);
  private readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>('dialog');

  protected readonly request = this.confirm.request;
  protected readonly typed = linkedSignal({ source: this.request, computation: () => '' });
  protected readonly canConfirm = computed(() => {
    const req = this.request();
    if (!req) return false;
    return !req.typedConfirmation || this.typed().trim() === req.typedConfirmation;
  });

  constructor() {
    effect(() => {
      const el = this.dialog().nativeElement;
      if (this.request()) {
        if (!el.open) el.showModal?.();
      } else if (el.open) {
        el.close();
      }
    });
  }

  protected answer(confirmed: boolean): void {
    const req = this.request();
    if (!req) return;
    if (confirmed && !this.canConfirm()) return;
    req.resolve(confirmed);
  }
}
