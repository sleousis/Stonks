import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  ViewEncapsulation,
  effect,
  input,
  model,
  output,
  signal,
  viewChild,
} from '@angular/core';

/**
 * The modal sheet every confirmation uses: a `<dialog>` that is a centred
 * card on larger screens and a full-screen sheet on phones, with the actions
 * pinned to the bottom. The host renders its form inside and keeps the
 * state; the sheet only opens, closes and reports Escape.
 *
 *   <app-sheet [open]="!!req()" labelledBy="x-title" describedBy="x-message" (dismiss)="no()">
 *     <form class="sheet-form">
 *       <h2 id="x-title">…</h2> <p id="x-message" class="sheet-message">…</p>
 *       <div class="sheet-actions">…</div>
 *     </form>
 *   </app-sheet>
 *
 * Styles are not encapsulated so they reach the projected form; every
 * selector is scoped under `app-sheet`.
 */
@Component({
  selector: 'app-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None,
  template: `
    <dialog
      #dialog
      class="sheet"
      [class.sheet-wide]="wide()"
      [attr.aria-labelledby]="labelledBy()"
      [attr.aria-describedby]="describedBy()"
      (cancel)="$event.preventDefault(); dismiss.emit()"
      (close)="onNativeClose()"
    >
      <ng-content />
    </dialog>
  `,
  styles: `
    @use 'breakpoints' as bp;

    app-sheet {
      display: contents;
    }
    app-sheet .sheet {
      width: min(460px, calc(100vw - 32px));
      max-height: calc(100dvh - 32px);
      padding: 0;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-lg);
      background: var(--color-surface);
      color: var(--color-ink);
      box-shadow: var(--shadow-2);
    }
    app-sheet .sheet.sheet-wide {
      width: min(520px, calc(100vw - 32px));
    }
    app-sheet .sheet::backdrop {
      background: var(--color-scrim);
    }
    app-sheet .sheet-form {
      display: grid;
      gap: var(--space-4);
      padding: var(--space-5);
    }
    app-sheet .sheet-form h2 {
      font-size: var(--text-lg);
      overflow-wrap: anywhere;
    }
    app-sheet .sheet-message {
      color: var(--color-ink-2);
    }
    app-sheet .sheet-actions {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: var(--space-2);
    }
    @include bp.phone {
      app-sheet .sheet,
      app-sheet .sheet.sheet-wide {
        width: 100vw;
        max-width: 100vw;
        height: 100dvh;
        max-height: 100dvh;
        margin: 0;
        border: 0;
        border-radius: 0;
      }
      app-sheet .sheet-form {
        min-height: 100%;
        align-content: start;
        padding: calc(var(--space-5) + env(safe-area-inset-top)) var(--space-4)
          calc(var(--space-4) + env(safe-area-inset-bottom));
      }
      app-sheet .sheet-actions {
        margin-top: auto;
        flex-direction: column-reverse;
      }
      app-sheet .sheet-actions > * {
        width: 100%;
      }
    }
  `,
})
export class Sheet {
  readonly open = input.required<boolean>();
  readonly labelledBy = input<string | null>(null);
  readonly describedBy = input<string | null>(null);
  /** A wider card on larger screens (forms with more than one field). */
  readonly wide = input(false);
  /** Escape was pressed; the host decides what that means. */
  readonly dismiss = output<void>();

  private readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>('dialog');
  /** Bumped when the browser closes the dialog on its own. */
  private readonly nativeCloses = signal(0);

  constructor() {
    effect(() => {
      const el = this.dialog().nativeElement;
      this.nativeCloses();
      if (this.open()) {
        if (!el.open) el.showModal?.();
      } else if (el.open) {
        el.close();
      }
    });
  }

  /**
   * The browser closed the dialog itself: a second Escape or back gesture
   * cannot be cancelled. Report it, and open again if the host still wants
   * the sheet (a request under way), so its state and the screen agree and
   * the next open is not a no-op.
   */
  protected onNativeClose(): void {
    if (!this.open()) return;
    if (!this.dialog().nativeElement.open) {
      this.dismiss.emit();
      this.nativeCloses.update((n) => n + 1);
    }
  }
}

/**
 * "Type <phrase> to confirm": the field a sheet shows for actions that must
 * not happen by accident (real ticks, overrides). `matches` tells the host
 * when the confirm button may be enabled.
 *
 *   <app-typed-confirm inputId="x-typed" [phrase]="req.typedConfirmation" [(value)]="typed" />
 */
@Component({
  selector: 'app-typed-confirm',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="field">
      <label [for]="inputId()">
        Type <strong class="typed-phrase">{{ phrase() }}</strong> to confirm
      </label>
      <input
        class="input"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
        [id]="inputId()"
        [value]="value()"
        (input)="value.set($any($event.target).value)"
      />
    </div>
  `,
  styles: `
    :host {
      display: block;
    }
    .typed-phrase {
      font-weight: var(--weight-bold);
      user-select: all;
      overflow-wrap: anywhere;
    }
  `,
})
export class TypedConfirm {
  readonly phrase = input.required<string>();
  readonly inputId = input.required<string>();
  readonly value = model('');
}

/** True when `typed` is the phrase (surrounding spaces ignored), or no phrase is asked. */
export function typedMatches(phrase: string | null | undefined, typed: string): boolean {
  return !phrase || typed.trim() === phrase;
}
