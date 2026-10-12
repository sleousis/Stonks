import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  computed,
  input,
  model,
  viewChildren,
} from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

/** One tab: a page of its own (`path`), or a section of this page (`id`). */
export interface PageTab {
  label: string;
  /** A route: the tab is a link, marked with aria-current when it is the page. */
  path?: string;
  /** Match the route exactly (default true). */
  exact?: boolean;
  /** Query params for a `path` tab. */
  queryParams?: Record<string, string>;
  /** A section of this page: the tab is a button in a tablist. */
  id?: string;
  /** A count beside the label ("Waiting for you 2"); hidden when null or 0. */
  badge?: number | null;
}

/**
 * The one tab style of the console (M4): a 2px ink underline under the
 * current tab over a 1px rule, 44px tall on phones and touch screens, scrolling sideways with a
 * fading edge when the tabs do not fit. Two kinds, one look:
 *
 * - Links, when each view has its own address (Orders, Notifications):
 *
 *     <app-page-tabs label="Orders views" [tabs]="[{ label: 'Fills', path: '/orders/fills' }]" />
 *
 * - A tablist, when the sections share one page. The page renders each
 *   panel with `role="tabpanel"` and `[attr.aria-labelledby]="tabs.tabId(id)"`:
 *
 *     <app-page-tabs #tabs idPrefix="settings" label="Settings sections"
 *                    [tabs]="sections" [(selected)]="section" />
 *
 * Segmented controls stay for filters and view switches inside a panel.
 *
 * `variant="sub"` is a second tab row under the first (Journal): filter
 * pills, the current one filled with ink, so it reads as sub-navigation.
 */
@Component({
  selector: 'app-page-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  host: { '[attr.data-variant]': 'variant()' },
  template: `
    @if (asLinks()) {
      <nav class="tabs" [attr.aria-label]="label()">
        @for (t of tabs(); track t.path ?? t.id) {
          <a
            class="tab"
            [routerLink]="t.path"
            [queryParams]="t.queryParams"
            routerLinkActive="active"
            ariaCurrentWhenActive="page"
            [routerLinkActiveOptions]="{ exact: t.exact ?? true }"
            >{{ t.label }}</a
          >
        }
      </nav>
    } @else {
      <div class="tabs" role="tablist" [attr.aria-label]="label()">
        @for (t of tabs(); track t.id; let i = $index) {
          <button
            #tab
            type="button"
            role="tab"
            class="tab"
            [id]="tabId(t.id!)"
            [class.active]="t.id === selected()"
            [attr.aria-selected]="t.id === selected()"
            [attr.aria-controls]="panelId(t.id!)"
            [tabIndex]="t.id === selected() ? 0 : -1"
            (click)="selected.set(t.id!)"
            (keydown)="onKey($event, i)"
          >
            {{ t.label }}
            @if (t.badge) {
              <span class="badge num">{{ t.badge }}</span>
            }
          </button>
        }
      </div>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: block;
      min-width: 0;
      margin: calc(-1 * var(--space-2)) 0 var(--space-6);
    }
    .tabs {
      display: flex;
      gap: var(--space-5);
      border-bottom: 1px solid var(--rule);
      overflow-x: auto;
      scrollbar-width: none;
      /* A fading right edge hints at tabs past the edge (m9). */
      mask-image: linear-gradient(to right, #000 calc(100% - 24px), transparent);
    }
    .tabs::-webkit-scrollbar {
      display: none;
    }
    .tab {
      display: inline-flex;
      align-items: center;
      min-height: 40px;
      margin-bottom: -1px;
      padding: 0;
      border: 0;
      border-bottom: 2px solid transparent;
      background: none;
      color: var(--muted);
      font: inherit;
      font-size: var(--text-md);
      font-weight: var(--weight-label);
      text-decoration: none;
      white-space: nowrap;
      cursor: pointer;
      transition:
        color var(--dur-fast) var(--ease-out),
        border-color var(--dur-fast) var(--ease-out);
    }
    .tab:last-child {
      margin-right: 24px;
    }
    .badge {
      min-width: 1.25rem;
      margin-left: var(--space-2);
      padding: 0 6px;
      border-radius: var(--r-pill);
      background: var(--ink);
      color: var(--bg);
      font-size: var(--text-2xs);
      line-height: 18px;
      text-align: center;
    }
    .tab:hover {
      color: var(--ink);
    }
    .tab.active {
      border-bottom-color: var(--ink);
      color: var(--ink);
    }
    .tab:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: -2px;
    }
    /* Sub: a second-level row. No rule, the current tab a filter pill. */
    :host([data-variant='sub']) {
      margin-top: 0;
    }
    :host([data-variant='sub']) .tabs {
      gap: var(--space-1);
      border-bottom: 0;
    }
    :host([data-variant='sub']) .tab {
      min-height: 32px;
      margin-bottom: 0;
      padding: 0 var(--space-3);
      border: 1px solid var(--rule);
      border-radius: var(--r-pill);
      color: var(--ink-2);
      font-size: 0.8125rem;
    }
    :host([data-variant='sub']) .tab.active {
      border-color: var(--ink);
      background: var(--ink);
      color: var(--bg);
    }
    @include bp.phone {
      .tabs {
        gap: var(--space-4);
      }
      .tab,
      :host([data-variant='sub']) .tab {
        min-height: var(--touch-min);
      }
    }
    @include bp.coarse {
      .tab,
      :host([data-variant='sub']) .tab {
        min-height: var(--touch-min);
      }
    }
  `,
})
export class PageTabs {
  readonly tabs = input.required<readonly PageTab[]>();
  /** The accessible name of the tab bar ("Settings sections"). */
  readonly label = input.required<string>();
  /** Keeps ids unique when a page has two tab bars. */
  readonly idPrefix = input('tabs');
  /** The section shown, for tablist tabs. */
  readonly selected = model<string | null>(null);
  /** `main` (default): the page's tabs. `sub`: a second row under them. */
  readonly variant = input<'main' | 'sub'>('main');

  protected readonly asLinks = computed(() => this.tabs().every((t) => !!t.path));
  private readonly buttons = viewChildren<ElementRef<HTMLButtonElement>>('tab');

  tabId(id: string): string {
    return `${this.idPrefix()}-tab-${id}`;
  }

  panelId(id: string): string {
    return `${this.idPrefix()}-panel-${id}`;
  }

  /** Arrow keys move and pick (wrapping), Home and End jump to the ends. */
  protected onKey(event: KeyboardEvent, index: number): void {
    const count = this.tabs().length;
    let next: number | null = null;
    if (event.key === 'ArrowRight') next = (index + 1) % count;
    else if (event.key === 'ArrowLeft') next = (index - 1 + count) % count;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = count - 1;
    if (next === null) return;
    event.preventDefault();
    const tab = this.tabs()[next];
    if (tab.id) this.selected.set(tab.id);
    this.buttons()[next]?.nativeElement.focus();
  }
}
