import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterNextRender,
  inject,
  Injector,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { NavState } from './nav';
import { WorkspaceIcon } from './workspace-icon';

/**
 * The workspace switcher in the chrome bar: a button with the current
 * workspace, and a sheet with every workspace in a 2x2 grid (its icon,
 * name, a blurb and links to its pages). Picking a workspace goes to its
 * first page. Arrow keys move between workspaces, Enter opens one, Escape
 * closes and gives focus back to the button. With three workspaces the
 * third spans both columns, so no cell is left empty.
 */
@Component({
  selector: 'app-workspace-menu',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, WorkspaceIcon],
  host: {
    '(document:click)': 'onDocumentClick($event)',
    '(keydown.escape)': 'close(true)',
  },
  template: `
    <button
      #button
      type="button"
      class="chrome-control ws-button"
      data-open-nav
      aria-haspopup="dialog"
      [attr.aria-expanded]="open()"
      aria-controls="workspace-menu"
      (click)="toggle()"
    >
      <span class="ws-glyph"><app-workspace-icon /></span>
      <span class="ws-name">{{ state.current() }}</span>
      <span class="visually-hidden">, switch workspace</span>
      <svg class="caret" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6">
        <path d="m3 4.5 3 3 3-3" />
      </svg>
    </button>
    @if (open()) {
      <div
        id="workspace-menu"
        class="chrome-sheet ws-menu"
        role="dialog"
        aria-label="Workspaces"
        (keydown)="onKey($event)"
      >
        @for (ws of state.workspaces(); track ws.name) {
          <div class="cell" [class.current]="ws.name === state.current()">
            <a
              class="head"
              [routerLink]="ws.items[0].path"
              [attr.aria-current]="ws.name === state.current() ? 'true' : null"
              (click)="close()"
            >
              <span class="icon"><app-workspace-icon [name]="ws.name" /></span>
              <span class="words">
                <span class="name">{{ ws.name }}</span>
                <span class="blurb">{{ ws.blurb }}</span>
              </span>
            </a>
            <ul class="pages" [attr.aria-label]="ws.name + ' pages'">
              @for (item of ws.items; track item.path) {
                <li>
                  <a
                    [routerLink]="item.path"
                    [attr.aria-current]="state.isActive(item) ? 'page' : null"
                    [attr.aria-label]="state.spoken(item)"
                    (click)="close()"
                    >{{ item.label }}</a
                  >
                </li>
              }
            </ul>
          </div>
        }
      </div>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: contents;
    }
    .ws-glyph {
      display: inline-grid;
      place-items: center;
      width: 20px;
      height: 20px;
      padding: 3px;
      border-radius: 5px;
      background: color-mix(in srgb, var(--chrome-accent) 22%, var(--chrome-2));
      color: var(--chrome-accent);
    }
    .ws-glyph app-workspace-icon {
      width: 14px;
      height: 14px;
    }
    .ws-menu {
      left: calc(var(--gutter) + 96px);
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: var(--space-1);
      width: 640px;
      max-width: calc(100vw - 16px);
    }
    .cell {
      position: relative;
      display: grid;
      gap: var(--space-1);
      align-content: start;
      padding: var(--space-3);
      border-radius: var(--r-ctl);
    }
    .cell:last-child:nth-child(odd) {
      grid-column: 1 / -1;
    }
    .cell:hover {
      background: var(--sunk);
    }
    .cell.current {
      background: var(--accent-soft);
      box-shadow: inset 3px 0 0 var(--accent);
    }
    .head {
      display: flex;
      gap: var(--space-3);
      color: var(--ink);
      text-decoration: none;
      border-radius: var(--r-tag);
    }
    .icon {
      display: grid;
      place-items: center;
      flex: none;
      width: 32px;
      height: 32px;
      padding: 7px;
      border-radius: 8px;
      background: var(--sunk);
      color: var(--accent);
    }
    .cell.current .icon {
      background: var(--raise);
    }
    .words {
      display: grid;
      gap: 2px;
    }
    .name {
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
    }
    .blurb {
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .pages {
      display: flex;
      flex-wrap: wrap;
      gap: 0 var(--space-3);
      margin: 0;
      padding: 0 0 0 44px;
      list-style: none;
    }
    .pages a {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      color: var(--muted);
      font-size: var(--text-sm);
      text-decoration: none;
    }
    .pages a:hover,
    .pages a[aria-current='page'] {
      color: var(--ink);
      text-decoration: underline;
    }
    @include bp.phone {
      .ws-menu {
        position: fixed;
        top: calc(52px + env(safe-area-inset-top));
        left: 8px;
        right: 8px;
        width: auto;
        max-width: none;
        max-height: calc(100dvh - 140px);
        overflow-y: auto;
        grid-template-columns: minmax(0, 1fr);
      }
      .cell:last-child:nth-child(odd) {
        grid-column: auto;
      }
      .head {
        min-height: var(--touch-min);
      }
      .pages a {
        min-height: var(--touch-min);
      }
    }
    @include bp.coarse {
      .pages a {
        min-height: var(--touch-min);
      }
    }
  `,
})
export class WorkspaceMenu {
  protected readonly state = inject(NavState);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  protected readonly open = signal(false);

  protected toggle(): void {
    if (this.open()) {
      this.close();
      return;
    }
    this.open.set(true);
    afterNextRender(() => this.heads()[this.currentIndex()]?.focus(), {
      injector: this.injector,
    });
  }

  close(returnFocus = false): void {
    if (!this.open()) return;
    this.open.set(false);
    if (returnFocus) this.host.nativeElement.querySelector<HTMLElement>('.ws-button')?.focus();
  }

  protected onDocumentClick(event: MouseEvent): void {
    if (this.open() && !this.host.nativeElement.contains(event.target as Node)) this.close();
  }

  /** Arrow keys move between workspaces: left and right by one, up and down by a row. */
  protected onKey(event: KeyboardEvent): void {
    const heads = this.heads();
    const at = heads.indexOf(document.activeElement as HTMLElement);
    if (at < 0) return;
    const cols = getComputedStyle(heads[0].closest('.ws-menu')!).gridTemplateColumns.split(
      ' ',
    ).length;
    const step: Record<string, number> = {
      ArrowRight: 1,
      ArrowLeft: -1,
      ArrowDown: cols,
      ArrowUp: -cols,
    };
    if (!(event.key in step)) return;
    event.preventDefault();
    const next = Math.min(heads.length - 1, Math.max(0, at + step[event.key]));
    heads[next].focus();
  }

  private heads(): HTMLElement[] {
    return [...this.host.nativeElement.querySelectorAll<HTMLElement>('.ws-menu .head')];
  }

  private currentIndex(): number {
    return Math.max(
      0,
      this.state.workspaces().findIndex((w) => w.name === this.state.current()),
    );
  }
}
