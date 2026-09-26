import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/**
 * The order side as a ticket mark: a solid "B" block for a buy, an outlined
 * "S" for a sell. Shape and letter carry the meaning, never colour alone;
 * screen readers hear "Buy" or "Sell".
 *
 *   <app-side-tag [side]="order.side" />
 */
@Component({
  selector: 'app-side-tag',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<span class="tag" [attr.data-side]="kind()" [attr.title]="word()"
    ><span aria-hidden="true">{{ letter() }}</span
    ><span class="visually-hidden">{{ word() }}</span></span
  >`,
  styles: `
    :host {
      display: inline-flex;
      vertical-align: middle;
    }
    .tag {
      display: inline-grid;
      place-items: center;
      width: 1.5rem;
      height: 1.5rem;
      border: 1.5px solid var(--color-ink);
      border-radius: var(--radius-sm);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: var(--text-xs);
      font-weight: var(--weight-bold);
      line-height: 1;
    }
    .tag[data-side='buy'] {
      background: var(--color-ink);
      color: var(--color-surface);
    }
    .tag[data-side='sell'] {
      background: transparent;
      color: var(--color-ink);
    }
    .tag[data-side='other'] {
      border-style: dashed;
      color: var(--color-ink-2);
    }
  `,
})
export class SideTag {
  /** `buy` / `sell` (any case); anything else shows its first letter. */
  readonly side = input.required<string | null | undefined>();

  protected readonly kind = computed(() => {
    const s = (this.side() ?? '').toLowerCase();
    return s === 'buy' || s === 'sell' ? s : 'other';
  });
  protected readonly letter = computed(() => {
    const k = this.kind();
    if (k === 'buy') return 'B';
    if (k === 'sell') return 'S';
    return (this.side() ?? '?').charAt(0).toUpperCase() || '?';
  });
  protected readonly word = computed(() => {
    const k = this.kind();
    if (k === 'buy') return 'Buy';
    if (k === 'sell') return 'Sell';
    return this.side() ?? 'Unknown side';
  });
}
