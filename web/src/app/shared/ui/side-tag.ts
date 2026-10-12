import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/**
 * The order side as an 18px tag: a green "B" for a buy, a red "S" for a
 * sell. The letter carries the meaning, never colour alone; screen readers
 * hear "Buy" or "Sell".
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
      width: 18px;
      height: 18px;
      border-radius: var(--r-tag);
      font-family: var(--font-mono);
      font-size: var(--text-2xs);
      font-weight: 700;
      line-height: 1;
    }
    .tag[data-side='buy'] {
      background: var(--up-soft);
      color: var(--up);
    }
    .tag[data-side='sell'] {
      background: var(--down-soft);
      color: var(--down);
    }
    .tag[data-side='other'] {
      border: 1px dashed var(--rule-control);
      color: var(--ink-2);
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
