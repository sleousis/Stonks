import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';

import { WIKI_GLOSSARY_URL, findGlossary } from '../../core/help/glossary';

let nextId = 0;

/**
 * A small "?" button that explains a metric in one plain sentence, with a
 * link to the wiki glossary. `term` is a glossary key or a visible label
 * ("Max drawdown", "max_drawdown"); unknown terms render nothing, so it is
 * safe to drop next to any label.
 *
 *   <dt>{{ m.label }} <app-help-tip [term]="m.key" /></dt>
 *
 * Opens on click or tap (never on hover alone), closes on Escape, outside
 * click or a second click. Uses the native popover so it sits in the top
 * layer above tables and sticky headers. The text renders only while open,
 * so labels' text content stays just the label.
 */
@Component({
  selector: 'app-help-tip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[class.empty]': '!match()' },
  template: `
    @if (match(); as m) {
      <button
        #trigger
        type="button"
        class="trigger"
        [attr.popovertarget]="id"
        [attr.aria-label]="'What is ' + m.entry.term + '?'"
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <circle cx="8" cy="8" r="6.6" fill="none" stroke="currentColor" stroke-width="1.3" />
          <path
            d="M6.3 6.2a1.8 1.8 0 1 1 2.5 1.7c-.5.2-.8.6-.8 1.1v.4"
            fill="none"
            stroke="currentColor"
            stroke-width="1.3"
            stroke-linecap="round"
          />
          <circle cx="8" cy="11.5" r=".8" fill="currentColor" />
        </svg>
      </button>
      <span
        #panel
        class="panel-tip"
        popover
        role="dialog"
        [id]="id"
        [attr.aria-label]="m.entry.term"
        (toggle)="onToggle($event)"
      >
        @if (open()) {
          <span class="term">{{ m.entry.term }}</span>
          <span class="text">{{ m.entry.short }}</span>
          <a class="more" [href]="wikiUrl" target="_blank" rel="noopener">
            More in the glossary<span class="visually-hidden"> (opens in a new tab)</span>
          </a>
        }
      </span>
    }
  `,
  styles: `
    :host {
      display: inline-flex;
      vertical-align: middle;
    }
    :host(.empty) {
      display: none;
    }
    .trigger {
      position: relative;
      display: inline-grid;
      place-items: center;
      /* 24px: the WCAG 2.2 minimum target on fine pointers. */
      width: 24px;
      height: 24px;
      margin: -3px 0;
      padding: 0;
      border: 0;
      border-radius: 50%;
      background: none;
      color: var(--color-ink-3);
      cursor: help;
    }
    /* A 44px hit area on touch without changing the layout. */
    @media (pointer: coarse) {
      .trigger::after {
        content: '';
        position: absolute;
        inset: -10px;
      }
    }
    .trigger:hover,
    .trigger[aria-expanded='true'] {
      color: var(--color-brass);
    }
    .panel-tip:popover-open {
      display: block;
    }
    .panel-tip {
      position: fixed;
      inset: auto;
      margin: 0;
      width: min(300px, calc(100vw - 32px));
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border-strong);
      border-radius: var(--radius-md);
      background: var(--color-surface);
      color: var(--color-ink);
      box-shadow: var(--shadow-2);
      font-size: var(--text-sm);
      font-weight: var(--weight-regular);
      line-height: var(--leading-body);
      text-align: start;
      white-space: normal;
    }
    .term,
    .text {
      display: block;
    }
    .term {
      font-weight: var(--weight-semibold);
    }
    .text {
      margin-top: var(--space-1);
      color: var(--color-ink-2);
    }
    .more {
      display: inline-block;
      margin-top: var(--space-2);
      color: var(--color-primary);
    }
  `,
})
export class HelpTip {
  /** Glossary key or visible label. */
  readonly term = input<string | null | undefined>(null);

  protected readonly match = computed(() => findGlossary(this.term()));
  protected readonly id = `help-tip-${nextId++}`;
  protected readonly wikiUrl = WIKI_GLOSSARY_URL;

  protected readonly open = signal(false);

  private readonly injector = inject(Injector);
  private readonly trigger = viewChild<ElementRef<HTMLButtonElement>>('trigger');
  private readonly panel = viewChild<ElementRef<HTMLElement>>('panel');

  protected onToggle(event: Event): void {
    const open = (event as Event & { newState?: string }).newState === 'open';
    this.open.set(open);
    if (open) afterNextRender(() => this.place(), { injector: this.injector });
  }

  /** Place the panel under the button (above if no room), inside the viewport. */
  private place(): void {
    const button = this.trigger()?.nativeElement;
    const panel = this.panel()?.nativeElement;
    const view = button?.ownerDocument.defaultView;
    if (!button || !panel || !view) return;
    const gap = 6;
    const edge = 16;
    const b = button.getBoundingClientRect();
    const p = panel.getBoundingClientRect();
    const left = Math.min(
      Math.max(edge, b.left + b.width / 2 - p.width / 2),
      view.innerWidth - p.width - edge,
    );
    const below = b.bottom + gap;
    const top =
      below + p.height > view.innerHeight - edge ? Math.max(edge, b.top - gap - p.height) : below;
    panel.style.left = `${Math.round(left)}px`;
    panel.style.top = `${Math.round(top)}px`;
  }
}
