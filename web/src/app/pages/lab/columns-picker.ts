import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  output,
} from '@angular/core';

export interface PickableColumn {
  key: string;
  label: string;
  /** Always shown (the name column): not offered in the picker. */
  fixed?: boolean;
}

function readStored(key: string): string[] | null {
  try {
    const raw = localStorage.getItem(key);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    return Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === 'string') : null;
  } catch {
    return null;
  }
}

function writeStored(key: string, hidden: readonly string[]): void {
  try {
    localStorage.setItem(key, JSON.stringify(hidden));
  } catch {
    // Private windows and blocked storage: the choice lasts for this visit.
  }
}

/** The columns left after hiding some, in their order. */
export function visibleColumns<T extends { key: string }>(
  columns: readonly T[],
  hidden: readonly string[],
): T[] {
  return columns.filter((c) => !hidden.includes(c.key));
}

/**
 * A "Columns" menu for wide research tables: tick the columns to show, so a
 * table fits its width instead of scrolling sideways. The choice is a
 * per-viewer convenience kept in this browser (`storageKey`).
 *
 *   <app-columns-picker [columns]="pickable" storageKey="lab.ledger.columns"
 *     [defaultHidden]="['n_failed']" (hiddenChange)="hidden.set($event)" />
 */
@Component({
  selector: 'app-columns-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '(document:click)': 'onDocumentClick($event)' },
  template: `
    <details class="picker" #menu>
      <summary class="btn btn-compact">
        <svg
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          stroke-linecap="round"
          aria-hidden="true"
        >
          <path d="M2.5 3.5h11M2.5 8h11M2.5 12.5h11" />
          <path d="M6 2v3M10 6.5v3M5 11v3" />
        </svg>
        Columns
        @if (hiddenCount()) {
          <span class="count-badge" [attr.aria-label]="hiddenCount() + ' hidden'">{{
            hiddenCount()
          }}</span>
        }
      </summary>
      <fieldset class="menu">
        <legend class="visually-hidden">Columns to show</legend>
        @for (c of offered(); track c.key) {
          <label class="check">
            <input
              type="checkbox"
              [checked]="!hidden().includes(c.key)"
              (change)="toggle(c.key, $any($event.target).checked)"
            />
            {{ c.label }}
          </label>
        }
      </fieldset>
    </details>
  `,
  styles: `
    :host {
      position: relative;
      display: inline-block;
    }
    summary {
      list-style: none;
    }
    summary::-webkit-details-marker {
      display: none;
    }
    .menu {
      position: absolute;
      top: calc(100% + 6px);
      right: 0;
      z-index: var(--z-menu);
      display: grid;
      min-width: 13rem;
      margin: 0;
      padding: var(--space-2) var(--space-3);
      border: 1px solid var(--rule);
      border-radius: var(--r-sheet);
      background: var(--raise);
      box-shadow: var(--shadow-overlay);
      animation: menu-in var(--dur-menu) var(--ease-out);
      transform-origin: top right;
    }
    .check {
      font-size: var(--text-md);
      white-space: nowrap;
    }
  `,
})
export class ColumnsPicker {
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  readonly columns = input.required<readonly PickableColumn[]>();
  /** Where this browser keeps the choice. */
  readonly storageKey = input.required<string>();
  /** Hidden until the viewer picks otherwise. */
  readonly defaultHidden = input<readonly string[]>([]);
  readonly hiddenChange = output<string[]>();

  protected readonly hidden = linkedSignal<string[]>(
    () => readStored(this.storageKey()) ?? [...this.defaultHidden()],
  );
  protected readonly offered = computed(() => this.columns().filter((c) => !c.fixed));
  protected readonly hiddenCount = computed(
    () => this.hidden().filter((k) => this.offered().some((c) => c.key === k)).length,
  );

  constructor() {
    effect(() => this.hiddenChange.emit(this.hidden()));
  }

  protected toggle(key: string, show: boolean): void {
    const next = show ? this.hidden().filter((k) => k !== key) : [...this.hidden(), key];
    this.hidden.set(next);
    writeStored(this.storageKey(), next);
  }

  /** A click outside closes the menu. */
  protected onDocumentClick(event: Event): void {
    const details = this.host.nativeElement.querySelector('details');
    if (details?.open && !this.host.nativeElement.contains(event.target as Node)) {
      details.open = false;
    }
  }
}
