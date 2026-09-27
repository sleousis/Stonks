import { ChangeDetectionStrategy, Component, computed, inject, linkedSignal } from '@angular/core';

import { ConfirmService } from '../../core/confirm/confirm.service';
import { ModeStamp } from './mode-stamp';
import { Sheet, TypedConfirm, typedMatches } from './sheet';
import { SideTag } from './side-tag';

/**
 * Renders ConfirmService requests in a modal sheet. Mounted once, in the
 * shell. With a `ticket`, the request reads as an order ticket: side mark,
 * PAPER or LIVE, and the lines in tabular mono.
 */
@Component({
  selector: 'app-confirm-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Sheet, TypedConfirm, SideTag, ModeStamp],
  template: `
    <app-sheet
      [open]="!!request()"
      labelledBy="confirm-title"
      describedBy="confirm-message"
      (dismiss)="answer(false)"
    >
      @if (request(); as req) {
        <form class="sheet-form" method="dialog" (submit)="$event.preventDefault(); answer(true)">
          <h2 id="confirm-title">{{ req.title }}</h2>
          @if (req.ticket; as t) {
            <div class="ticket" [class.live]="t.live" aria-label="Order ticket" role="group">
              <p class="ticket-head">
                <span class="ticket-kind">Order ticket</span>
                @if (t.side) {
                  <app-side-tag [side]="t.side" />
                }
                <app-mode-stamp [live]="t.live" />
              </p>
              <dl class="ticket-lines">
                @for (line of t.lines; track line.label) {
                  <div>
                    <dt>{{ line.label }}</dt>
                    <dd class="num">{{ line.value }}</dd>
                  </div>
                }
              </dl>
            </div>
          }
          <p id="confirm-message" class="sheet-message">{{ req.message }}</p>
          @if (req.typedConfirmation) {
            <app-typed-confirm
              inputId="confirm-typed"
              [phrase]="req.typedConfirmation"
              [(value)]="typed"
            />
          }
          <div class="sheet-actions">
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
    </app-sheet>
  `,
})
export class ConfirmDialog {
  private readonly confirm = inject(ConfirmService);

  protected readonly request = this.confirm.request;
  protected readonly typed = linkedSignal({ source: this.request, computation: () => '' });
  protected readonly canConfirm = computed(() => {
    const req = this.request();
    return !!req && typedMatches(req.typedConfirmation, this.typed());
  });

  protected answer(confirmed: boolean): void {
    const req = this.request();
    if (!req) return;
    if (confirmed && !this.canConfirm()) return;
    req.resolve(confirmed);
  }
}
