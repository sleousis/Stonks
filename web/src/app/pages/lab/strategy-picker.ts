import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterRenderEffect,
  computed,
  inject,
  input,
  model,
  signal,
} from '@angular/core';

import type { StrategyClassInfo } from '../../api/models';
import { assetClassWords, groupStrategies, strategyTitle } from './lab-requests';

/**
 * Pick a strategy from the catalog: a search box over its plain name and
 * description, then radio buttons grouped by the kind of idea, each with
 * one plain line on what it does. Native radios keep keyboard and screen-reader
 * behaviour for free.
 */
@Component({
  selector: 'app-strategy-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <fieldset
      class="picker"
      [attr.aria-describedby]="error() ? idPrefix() + '-strategy-error' : null"
    >
      <legend>Strategy</legend>
      <input
        class="input search"
        type="search"
        autocomplete="off"
        spellcheck="false"
        placeholder="Search by name or description"
        [attr.aria-label]="'Search ' + classes().length + ' strategies'"
        [attr.aria-controls]="idPrefix() + '-strategy-list'"
        [value]="query()"
        (input)="query.set($any($event.target).value)"
      />
      <div class="list" [id]="idPrefix() + '-strategy-list'" tabindex="-1">
        @for (g of groups(); track g.label) {
          <div class="group" role="group" [attr.aria-label]="g.label">
            <p class="group-label" aria-hidden="true">{{ g.label }}</p>
            @for (c of g.classes; track c.class_path) {
              <label class="option" [class.selected]="value() === c.class_path">
                <input
                  type="radio"
                  [name]="idPrefix() + '-strategy'"
                  [value]="c.class_path"
                  [checked]="value() === c.class_path"
                  (change)="value.set(c.class_path)"
                />
                <span class="text">
                  <span class="name">{{ title(c) }}</span>
                  @if (c.description) {
                    <span class="desc">{{ c.description }}</span>
                  }
                  <span class="tags">
                    {{ assets(c.applicable_asset_classes) }} ·
                    {{ c.parameters.length }}
                    {{ c.parameters.length === 1 ? 'setting' : 'settings' }}
                  </span>
                </span>
              </label>
            }
          </div>
        } @empty {
          <p class="muted none">No strategy matches “{{ query() }}”.</p>
        }
      </div>
      @if (error()) {
        <span class="error" [id]="idPrefix() + '-strategy-error'">{{ error() }}</span>
      }
    </fieldset>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .picker {
      margin: 0;
      padding: 0;
      border: 0;
      min-width: 0;
      display: grid;
      gap: var(--space-2);
    }
    legend {
      padding: 0;
      margin-bottom: var(--space-1);
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .desc {
      max-width: 70ch;
    }
    /* The list is an inset well: no border, a hairline between rows, the
       picked row a surface chip with an accent rule. */
    .list {
      max-height: 18rem;
      overflow-y: auto;
      overscroll-behavior: contain;
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
    }
    .group + .group {
      border-top: 1px solid var(--color-border);
    }
    /* Group labels (Yardsticks, Trend following): the one overline style. */
    .group-label {
      position: sticky;
      top: 0;
      z-index: 1;
      padding: var(--space-2) var(--space-3) var(--space-1);
      font-size: var(--text-xs);
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: var(--tracking-overline);
      color: var(--color-ink-3);
      background: var(--color-surface-2);
    }
    .option {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: var(--space-2);
      align-items: start;
      min-height: var(--touch-min);
      padding: var(--space-2) var(--space-3);
      cursor: pointer;
      box-shadow: inset 2px 0 0 transparent;
      transition:
        background-color var(--dur-fast) var(--ease),
        box-shadow var(--dur-fast) var(--ease);
    }
    .option + .option {
      border-top: 1px solid var(--color-border);
    }
    .option:hover {
      background: var(--color-hover);
    }
    .option.selected {
      background: var(--color-surface);
      box-shadow: inset 2px 0 0 var(--color-accent);
    }
    input[type='radio'] {
      margin: 2px 0 0;
      width: 16px;
      height: 16px;
      accent-color: var(--color-accent);
    }
    @media (pointer: coarse) {
      input[type='radio'] {
        width: 18px;
        height: 18px;
      }
    }
    .text {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .name {
      font-size: var(--text-sm);
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    .desc {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .tags {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .none {
      padding: var(--space-3);
      font-size: var(--text-sm);
    }
    .error {
      font-size: var(--text-xs);
      color: var(--color-loss);
    }
  `,
})
export class StrategyPicker {
  readonly classes = input.required<readonly StrategyClassInfo[]>();
  readonly value = model.required<string>();
  readonly idPrefix = input('strategy');
  readonly error = input<string | null>(null);

  protected readonly query = signal('');
  protected readonly groups = computed(() => groupStrategies(this.classes(), this.query()));
  protected readonly title = strategyTitle;
  protected readonly assets = assetClassWords;

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  constructor() {
    // A strategy picked from outside ("Test again in Lab") scrolls into view
    // inside the list, never moving the page.
    afterRenderEffect(() => {
      const value = this.value();
      if (!value) return;
      const list = this.host.nativeElement.querySelector<HTMLElement>('.list');
      const option = list
        ?.querySelector<HTMLInputElement>(`input[type="radio"]:checked`)
        ?.closest<HTMLElement>('.option');
      if (!list || !option) return;
      const top = option.offsetTop - list.offsetTop;
      if (top < list.scrollTop || top + option.offsetHeight > list.scrollTop + list.clientHeight) {
        list.scrollTop = Math.max(0, top - list.clientHeight / 3);
      }
    });
  }
}
