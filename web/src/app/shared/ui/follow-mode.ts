import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  input,
  output,
  viewChild,
} from '@angular/core';

import type { SubscriptionMode } from '../../api/subscriptions.service';
import { MODES, type ModeOption } from '../governance-labels';

let nextId = 0;

/**
 * How you follow a strategy, as one compact control (M8): a labelled select
 * with the four follow modes in the vocabulary's words. Modes the auto gate
 * keeps closed stay listed but cannot be picked, and `describedBy` points
 * at the line that says why. The parent owns the value: a change it refuses
 * (a cancelled ticket, an API error) leaves the select on the stored mode.
 *
 *   <app-follow-mode [value]="sub.mode" [locked]="['approve', 'auto']"
 *                    [label]="'Mode for ' + name" (changed)="setMode(sub, $event)" />
 */
@Component({
  selector: 'app-follow-mode',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <label class="visually-hidden" [for]="id">{{ label() }}</label>
    <select
      #select
      class="input"
      [id]="id"
      [value]="value()"
      [disabled]="disabled()"
      [attr.aria-describedby]="describedBy()"
      (change)="pick($any($event.target).value)"
    >
      @for (m of modes(); track m.value) {
        <option [value]="m.value" [selected]="m.value === value()" [disabled]="isLocked(m.value)">
          {{ m.label }}{{ isLocked(m.value) ? ' (locked)' : '' }}
        </option>
      }
    </select>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    select {
      width: 100%;
    }
  `,
})
export class FollowMode {
  /** The stored mode. */
  readonly value = input.required<SubscriptionMode>();
  /** The accessible name, e.g. "Mode for Momentum". */
  readonly label = input('Follow mode');
  /** Modes shown but not pickable now. */
  readonly locked = input<readonly SubscriptionMode[]>([]);
  readonly disabled = input(false);
  readonly describedBy = input<string | null>(null);
  readonly modes = input<readonly ModeOption[]>(MODES);
  /** A different mode was picked. */
  readonly changed = output<SubscriptionMode>();

  protected readonly id = `follow-mode-${nextId++}`;
  private readonly select = viewChild.required<ElementRef<HTMLSelectElement>>('select');

  protected isLocked(mode: SubscriptionMode): boolean {
    return mode !== this.value() && this.locked().includes(mode);
  }

  protected pick(mode: SubscriptionMode): void {
    // Snap back to the stored mode; the parent's new value re-renders it if the change goes through.
    this.select().nativeElement.value = this.value();
    if (mode !== this.value() && !this.isLocked(mode)) this.changed.emit(mode);
  }
}
