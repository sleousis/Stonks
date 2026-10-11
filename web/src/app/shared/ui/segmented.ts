import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  input,
  model,
} from '@angular/core';

export interface SegmentOption<T extends string = string> {
  value: T;
  label: string;
}

/**
 * One choice out of a few, shown side by side (UX-60). A radio group: the
 * picked option is the only one in the tab order, the arrow keys move and
 * pick (wrapping), Home and End jump to the ends. 44px tall on phones and
 * touch screens.
 *
 *   <app-segmented label="Group costs" [options]="groupings" [(value)]="grouping" />
 *
 * `emphasis="strong"` fills the picked option solid, for choices that must
 * not be misread (Buy or Sell on an order ticket).
 *
 * On phones, more than three options form an equal two-column grid (a last
 * odd option takes the whole row), so no option is left alone on a line.
 */
@Component({
  selector: 'app-segmented',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="segmented"
      role="radiogroup"
      [attr.aria-label]="label()"
      [attr.data-emphasis]="emphasis()"
      [attr.data-many]="options().length > 3 ? '' : null"
    >
      @for (o of options(); track o.value; let i = $index) {
        <button
          type="button"
          role="radio"
          class="segment"
          [attr.aria-checked]="o.value === value()"
          [tabIndex]="i === focusIndex() ? 0 : -1"
          (click)="pick(i)"
          (keydown)="onKey($event, i)"
        >
          {{ o.label }}
        </button>
      }
    </div>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: inline-block;
      min-width: 0;
      max-width: 100%;
    }
    .segmented {
      display: flex;
      flex-wrap: wrap;
      gap: 2px;
      padding: 2px;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
    }
    .segment {
      flex: 1 1 auto;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: calc(var(--control-h) - 4px);
      padding: 0 var(--space-3);
      border: 0;
      border-radius: var(--radius-xs);
      background: transparent;
      color: var(--color-ink-2);
      font: inherit;
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
      white-space: nowrap;
      cursor: pointer;
      transition:
        background-color var(--dur-fast) var(--ease),
        color var(--dur-fast) var(--ease);
    }
    .segment:hover {
      background: var(--color-hover);
      color: var(--color-ink);
    }
    /* Subtle: a surface chip with a strong ring, readable at a glance. */
    .segment[aria-checked='true'] {
      background: var(--color-surface);
      color: var(--color-ink);
      box-shadow: inset 0 0 0 1px var(--color-border-strong);
      font-weight: var(--weight-semibold);
    }
    /* Strong: the picked option is a solid block, like the B side tag (M13). */
    .segmented[data-emphasis='strong'] .segment[aria-checked='true'] {
      background: var(--color-slab);
      color: var(--color-slab-ink);
      box-shadow: none;
      font-weight: var(--weight-bold);
    }
    .segment:focus-visible {
      outline: 2px solid var(--color-focus);
      outline-offset: 2px;
    }
    @include bp.phone {
      :host {
        display: block;
      }
      .segment {
        min-height: var(--touch-min);
      }
      .segmented[data-many] {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .segmented[data-many] .segment:last-child:nth-child(odd) {
        grid-column: 1 / -1;
      }
      .segmented[data-many] .segment {
        white-space: normal;
      }
    }
    @include bp.coarse {
      .segment {
        min-height: var(--touch-min);
      }
    }
  `,
})
export class Segmented<T extends string = string> {
  /** The group's accessible name ("Group by", "Show"). */
  readonly label = input.required<string>();
  readonly options = input.required<readonly SegmentOption<T>[]>();
  readonly value = model.required<T>();
  /** `subtle` (default): a raised chip. `strong`: a solid block. */
  readonly emphasis = input<'subtle' | 'strong'>('subtle');

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  /** The picked option takes the tab stop; the first one when none is picked. */
  protected readonly focusIndex = computed(() =>
    Math.max(
      0,
      this.options().findIndex((o) => o.value === this.value()),
    ),
  );

  protected pick(index: number): void {
    const option = this.options()[index];
    if (option) this.value.set(option.value);
  }

  protected onKey(event: KeyboardEvent, index: number): void {
    const count = this.options().length;
    let next: number;
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        next = (index + 1) % count;
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        next = (index - 1 + count) % count;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = count - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    this.pick(next);
    this.host.nativeElement.querySelectorAll<HTMLButtonElement>('[role="radio"]')[next]?.focus();
  }
}
