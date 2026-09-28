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
 * The one tab style of the console (M4): an underline under the current
 * tab, 44px tall on phones and touch screens, scrolling sideways with a
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
 */
@Component({
  selector: 'app-page-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
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
      margin: calc(-1 * var(--space-2)) 0 var(--space-4);
    }
    .tabs {
      display: flex;
      gap: var(--space-1);
      border-bottom: 1px solid var(--color-border);
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
      min-height: 36px;
      margin-bottom: -1px;
      padding: 0 var(--space-3);
      border: 0;
      border-bottom: 2px solid transparent;
      background: none;
      color: var(--color-ink-2);
      font: inherit;
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
      text-decoration: none;
      white-space: nowrap;
      cursor: pointer;
      transition: color var(--dur-fast) var(--ease);
    }
    .tab:last-child {
      margin-right: 24px;
    }
    .badge {
      min-width: 1.25rem;
      margin-left: var(--space-2);
      padding: 0 var(--space-1);
      border-radius: var(--radius-pill, 999px);
      background: var(--color-ink);
      color: var(--color-surface);
      font-size: var(--text-xs);
      text-align: center;
    }
    .tab:hover {
      color: var(--color-ink);
    }
    .tab.active {
      color: var(--color-ink);
      font-weight: var(--weight-semibold);
      border-bottom-color: var(--color-accent);
    }
    .tab:focus-visible {
      outline: 2px solid var(--color-focus);
      outline-offset: -2px;
    }
    @include bp.phone {
      .tab {
        min-height: var(--touch-min);
        padding: 0 var(--space-2);
      }
    }
    @include bp.coarse {
      .tab {
        min-height: var(--touch-min);
      }
    }
    @media (prefers-reduced-motion: reduce) {
      .tab {
        transition: none;
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
