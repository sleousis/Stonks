import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  computed,
  signal,
  viewChild,
} from '@angular/core';

import { ModeStamp } from '../../shared/ui/mode-stamp';
import type { TickTicket } from './tick-confirm';

/**
 * A real trading run, confirmed on an order ticket: the PAPER or LIVE stamp,
 * the broker, date and tickers as ticket lines, and the broker label typed to
 * confirm. Hosted by the runner; `open()` resolves true when confirmed.
 */
@Component({
  selector: 'app-tick-ticket-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ModeStamp],
  template: `
    <dialog
      #dialog
      class="sheet"
      aria-labelledby="ticket-title"
      aria-describedby="ticket-message"
      (cancel)="$event.preventDefault(); answer(false)"
    >
      @if (ticket(); as t) {
        <form method="dialog" (submit)="$event.preventDefault(); answer(true)">
          <div class="ticket" [attr.data-mode]="t.live ? 'live' : 'paper'">
            <div class="ticket-head">
              <h2 id="ticket-title">{{ t.title }}</h2>
              <app-mode-stamp [live]="t.live" />
            </div>
            <dl class="lines">
              @for (line of t.lines; track line.label) {
                <div class="line">
                  <dt>{{ line.label }}</dt>
                  <dd [class.num]="line.mono">{{ line.value }}</dd>
                </div>
              }
            </dl>
          </div>
          <p id="ticket-message" class="message">{{ t.message }}</p>
          <div class="field">
            <label for="ticket-typed">
              Type <strong class="phrase">{{ t.typedConfirmation }}</strong> to confirm
            </label>
            <input
              id="ticket-typed"
              class="input"
              autocomplete="off"
              autocapitalize="off"
              spellcheck="false"
              [value]="typed()"
              (input)="typed.set($any($event.target).value)"
            />
          </div>
          <div class="actions">
            <button type="button" class="btn" (click)="answer(false)">Keep editing</button>
            <button type="submit" class="btn btn-danger" [disabled]="!canConfirm()">
              {{ t.confirmLabel }}
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
    .ticket {
      border: 1px dashed var(--color-border-strong);
      border-radius: var(--radius-md);
      background: var(--color-surface-2);
    }
    .ticket[data-mode='live'] {
      border-color: var(--color-brass);
    }
    .ticket-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border-bottom: 1px dashed var(--color-border-strong);
    }
    h2 {
      font-size: var(--text-lg);
    }
    .lines {
      display: grid;
      margin: 0;
      padding: var(--space-2) var(--space-4);
    }
    .line {
      display: flex;
      justify-content: space-between;
      gap: var(--space-3);
      padding: var(--space-1) 0;
      font-size: var(--text-sm);
    }
    dt {
      color: var(--color-ink-3);
    }
    dd {
      min-width: 0;
      margin: 0;
      font-weight: var(--weight-medium);
      text-align: right;
      overflow-wrap: anywhere;
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
export class TickTicketDialog {
  private readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>('dialog');
  protected readonly ticket = signal<TickTicket | null>(null);
  protected readonly typed = signal('');
  protected readonly canConfirm = computed(() => {
    const t = this.ticket();
    return !!t && this.typed().trim() === t.typedConfirmation;
  });
  private resolve: ((ok: boolean) => void) | null = null;

  /** Shows the ticket; true when the trader typed the label and confirmed. */
  open(ticket: TickTicket): Promise<boolean> {
    this.resolve?.(false);
    this.ticket.set(ticket);
    this.typed.set('');
    const el = this.dialog().nativeElement;
    if (!el.open) el.showModal?.();
    return new Promise<boolean>((resolve) => (this.resolve = resolve));
  }

  protected answer(confirmed: boolean): void {
    if (confirmed && !this.canConfirm()) return;
    const resolve = this.resolve;
    this.resolve = null;
    this.ticket.set(null);
    const el = this.dialog().nativeElement;
    if (el.open) el.close?.();
    resolve?.(confirmed);
  }
}
