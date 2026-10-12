import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterRenderEffect,
  inject,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { NavState } from './nav';

/**
 * The current workspace's pages as tabs in the chrome bar. The page on
 * screen carries aria-current and a 3px accent underline that slides to it.
 * The tab row is its own horizontal scroll container with faded edges, so
 * the page never scrolls sideways. Hidden on phones (the bottom nav and
 * the workspace menu take over).
 */
@Component({
  selector: 'app-workspace-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (state.currentView(); as ws) {
      <nav class="tabs" [attr.aria-label]="ws.name + ' pages'">
        @for (item of ws.items; track item.path) {
          <a
            class="tab"
            [routerLink]="item.path"
            [attr.aria-current]="state.isActive(item) ? 'page' : null"
            [attr.aria-label]="state.spoken(item)"
          >
            {{ item.label }}
            @if (state.badge(item); as n) {
              <b class="count" aria-hidden="true">{{ n }}</b>
            }
          </a>
        }
        <span
          class="bar"
          aria-hidden="true"
          [class.shown]="bar().width > 0"
          [style.width.px]="bar().width"
          [style.transform]="'translateX(' + bar().left + 'px)'"
        ></span>
      </nav>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: flex;
      flex: 0 1 auto;
      align-self: stretch;
      min-width: 0;
      margin-left: var(--space-1);
    }
    .tabs {
      position: relative;
      display: flex;
      align-items: stretch;
      min-width: 0;
      padding: 0 12px;
      overflow-x: auto;
      scrollbar-width: none;
      mask-image: linear-gradient(
        to right,
        transparent,
        #000 24px,
        #000 calc(100% - 24px),
        transparent
      );
    }
    .tabs::-webkit-scrollbar {
      display: none;
    }
    .tab {
      display: inline-flex;
      flex: none;
      align-items: center;
      gap: 6px;
      min-height: var(--touch-min);
      padding: 0 12px;
      color: var(--chrome-muted);
      font-size: var(--text-md);
      font-weight: var(--weight-medium);
      text-decoration: none;
      white-space: nowrap;
      transition: color var(--dur-fast) var(--ease-out);
    }
    .tab:hover,
    .tab[aria-current='page'] {
      color: var(--chrome-ink);
    }
    .tab:focus-visible {
      outline-color: var(--chrome-accent);
      outline-offset: -4px;
    }
    .count {
      min-width: 18px;
      padding: 0 5px;
      border-radius: var(--r-pill);
      background: var(--chrome-accent);
      color: #0a0e1f;
      font-family: var(--font-mono);
      font-size: var(--text-2xs);
      font-weight: var(--weight-semibold);
      line-height: 17px;
      text-align: center;
    }
    .bar {
      position: absolute;
      left: 0;
      bottom: 0;
      height: 3px;
      border-radius: 3px 3px 0 0;
      background: var(--chrome-accent);
      opacity: 0;
      transition:
        transform var(--dur-menu) var(--ease-out),
        width var(--dur-menu) var(--ease-out);
    }
    .bar.shown {
      opacity: 1;
    }
    @include bp.phone {
      :host {
        display: none;
      }
    }
  `,
})
export class WorkspaceTabs {
  protected readonly state = inject(NavState);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  /** Where the underline sits, measured from the current tab. */
  protected readonly bar = signal({ left: 0, width: 0 });

  constructor() {
    afterRenderEffect(() => {
      this.state.activeItem();
      this.state.currentView();
      const tab = this.host.nativeElement.querySelector<HTMLElement>('a[aria-current="page"]');
      const next = tab
        ? { left: tab.offsetLeft + 12, width: Math.max(0, tab.offsetWidth - 24) }
        : { left: 0, width: 0 };
      const now = this.bar();
      if (now.left !== next.left || now.width !== next.width) this.bar.set(next);
      tab?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    });
  }
}
