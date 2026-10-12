import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterRenderEffect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { NavState } from './nav';

/**
 * The current workspace's pages as tabs in the chrome bar. The page on
 * screen carries aria-current and a 3px accent underline that slides to it.
 * The tab row is its own horizontal scroll container, so the page never
 * scrolls sideways. When tabs hide past an edge, that edge fades and a
 * small arrow button scrolls the row (a mouse wheel cannot). The current
 * tab is scrolled to the middle. Hidden on phones (the bottom nav and the
 * workspace menu take over).
 */
@Component({
  selector: 'app-workspace-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (state.tabView(); as ws) {
      <nav
        #row
        class="tabs"
        [class.fade-start]="edges().start"
        [class.fade-end]="edges().end"
        [attr.aria-label]="ws.name + ' pages'"
        (scroll)="measure()"
      >
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
          [style.transform]="'translateX(' + bar().left + 'px) scaleX(' + bar().width + ')'"
        ></span>
      </nav>
      @if (edges().start) {
        <button
          type="button"
          class="nudge start"
          tabindex="-1"
          aria-hidden="true"
          (click)="nudge(-1)"
        >
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6">
            <path d="m12 5-5 5 5 5" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
      }
      @if (edges().end) {
        <button type="button" class="nudge end" tabindex="-1" aria-hidden="true" (click)="nudge(1)">
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6">
            <path d="m8 5 5 5-5 5" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
      }
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      position: relative;
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
      scroll-padding-inline: 40px;
    }
    /* Each edge fades only while tabs hide past it. */
    .tabs.fade-start {
      mask-image: linear-gradient(to right, transparent, #000 40px);
    }
    .tabs.fade-end {
      mask-image: linear-gradient(to left, transparent, #000 40px);
    }
    .tabs.fade-start.fade-end {
      mask-image: linear-gradient(
        to right,
        transparent,
        #000 40px,
        #000 calc(100% - 40px),
        transparent
      );
    }
    .nudge {
      position: absolute;
      top: 50%;
      display: grid;
      place-items: center;
      width: 24px;
      height: 32px;
      margin-top: -16px;
      padding: 0;
      border: 0;
      border-radius: var(--r-ctl);
      background: none;
      color: var(--chrome-muted);
      cursor: pointer;
    }
    .nudge:hover {
      background: var(--chrome-2);
      color: var(--chrome-ink);
    }
    .nudge.start {
      left: 0;
    }
    .nudge.end {
      right: 0;
    }
    .nudge svg {
      width: 16px;
      height: 16px;
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
    /* 1px wide, stretched by scaleX to the tab: the slide is transform only. */
    .bar {
      position: absolute;
      left: 0;
      bottom: 0;
      width: 1px;
      height: 3px;
      background: var(--chrome-accent);
      opacity: 0;
      transform-origin: left;
      transition: transform var(--dur-menu) var(--ease-out);
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
  /** Whether tabs hide past the start or end of the row. */
  protected readonly edges = signal({ start: false, end: false });
  private readonly row = viewChild<ElementRef<HTMLElement>>('row');

  constructor() {
    const resize =
      typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => this.measure());
    let watched: HTMLElement | null = null;
    inject(DestroyRef).onDestroy(() => resize?.disconnect());
    afterRenderEffect(() => {
      const el = this.row()?.nativeElement ?? null;
      if (el === watched) return;
      if (watched) resize?.unobserve(watched);
      if (el) resize?.observe(el);
      watched = el;
      this.measure();
    });
    afterRenderEffect(() => {
      this.state.activeItem();
      this.state.tabView();
      const tab = this.host.nativeElement.querySelector<HTMLElement>('a[aria-current="page"]');
      const next = tab
        ? { left: tab.offsetLeft + 12, width: Math.max(0, tab.offsetWidth - 24) }
        : { left: 0, width: 0 };
      const now = this.bar();
      if (now.left !== next.left || now.width !== next.width) this.bar.set(next);
      tab?.scrollIntoView?.({ block: 'nearest', inline: 'center' });
      this.measure();
    });
  }

  /** Reads the scroll position into `edges`; called on scroll and resize. */
  protected measure(): void {
    const el = this.row()?.nativeElement;
    const next = el
      ? {
          start: el.scrollLeft > 1,
          end: el.scrollLeft + el.clientWidth < el.scrollWidth - 1,
        }
      : { start: false, end: false };
    const now = this.edges();
    if (now.start !== next.start || now.end !== next.end) this.edges.set(next);
  }

  /** Scrolls the row most of a width toward one edge. */
  protected nudge(dir: 1 | -1): void {
    const el = this.row()?.nativeElement;
    if (!el) return;
    const reduce = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    el.scrollBy?.({ left: dir * el.clientWidth * 0.7, behavior: reduce ? 'auto' : 'smooth' });
  }
}
