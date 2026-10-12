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
 * behaviour for free. The list grows with the form (no inner scroll box):
 * the first six show, plus the picked one, and "Show all strategies" opens
 * the rest. A search always shows every match.
 */
/** Strategies shown before Show all strategies. */
const FIRST_SHOWN = 6;

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
        @for (g of shownGroups(); track g.label) {
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
      @if (hiddenCount() > 0) {
        <button type="button" class="btn btn-tertiary more" (click)="showAll.set(true)">
          Show all {{ classes().length }} strategies
        </button>
      }
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
      color: var(--ink-2);
      font-size: 0.8125rem;
      font-weight: var(--weight-label);
    }
    .desc {
      max-width: 70ch;
    }
    /* The list sits in a field edge and grows with the form: a hairline
       between rows, the picked row accent soft with a 3px accent edge. */
    .list {
      border: 1px solid var(--rule-control);
      border-radius: var(--r-ctl);
      background: var(--raise);
    }
    .group + .group {
      border-top: 1px solid var(--rule);
    }
    /* Group labels (Yardsticks, Trend following): small muted headers. */
    .group-label {
      padding: var(--space-2) var(--space-3) var(--space-1);
      font-size: var(--text-xs);
      font-weight: var(--weight-label);
      color: var(--muted);
      background: var(--sunk);
    }
    .option {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: var(--space-2);
      align-items: start;
      min-height: var(--touch-min);
      padding: var(--space-2) var(--space-3);
      cursor: pointer;
      box-shadow: inset 3px 0 0 transparent;
      transition:
        background-color var(--dur-fast) var(--ease-out),
        box-shadow var(--dur-fast) var(--ease-out);
    }
    .option + .option {
      border-top: 1px solid var(--rule);
    }
    .option:hover {
      background: var(--sunk);
    }
    .option.selected {
      background: var(--accent-soft);
      box-shadow: inset 3px 0 0 var(--accent);
    }
    input[type='radio'] {
      margin: 2px 0 0;
      width: 16px;
      height: 16px;
      accent-color: var(--accent);
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
      color: var(--ink-2);
    }
    .tags {
      font-size: var(--text-xs);
      color: var(--muted);
    }
    .none {
      padding: var(--space-3);
      font-size: var(--text-sm);
    }
    .more {
      justify-self: start;
    }
    .error {
      font-size: var(--text-xs);
      color: var(--down);
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
  /** Every strategy shows once asked, or while a search is typed. */
  protected readonly showAll = signal(false);
  /** The first few strategies and the picked one, unless all are shown. */
  protected readonly shownGroups = computed(() => {
    const groups = this.groups();
    if (this.showAll() || this.query().trim()) return groups;
    let left = FIRST_SHOWN;
    const picked = this.value();
    return groups
      .map((g) => ({
        ...g,
        classes: g.classes.filter((c) => left-- > 0 || c.class_path === picked),
      }))
      .filter((g) => g.classes.length > 0);
  });
  /** How many strategies the short list leaves out. */
  protected readonly hiddenCount = computed(() => {
    const all = this.groups().reduce((n, g) => n + g.classes.length, 0);
    const shown = this.shownGroups().reduce((n, g) => n + g.classes.length, 0);
    return all - shown;
  });
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
