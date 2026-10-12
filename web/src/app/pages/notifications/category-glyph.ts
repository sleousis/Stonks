import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/**
 * The line icon of a notification's category, in a 32px sunk squircle
 * (1.6 stroke, like every console icon). Decorative: the category word
 * beside it carries the meaning.
 */
@Component({
  selector: 'app-category-glyph',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { 'aria-hidden': 'true', '[attr.data-category]': 'category() ?? "general"' },
  template: `
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      stroke-width="1.6"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      @switch (category()) {
        @case ('signal') {
          <path d="M11 2.5 4.5 11H10l-1 6.5L15.5 9H10l1-6.5Z" />
        }
        @case ('price_alert') {
          <path d="M5 8a5 5 0 0 1 10 0c0 4 1.5 5.5 1.5 5.5h-13S5 12 5 8Z" />
          <path d="M8.5 16.5a1.6 1.6 0 0 0 3 0" />
        }
        @case ('event_alert') {
          <rect x="3" y="4.5" width="14" height="12" rx="2" />
          <path d="M3 8.5h14M7 2.5v3M13 2.5v3" />
        }
        @case ('screen_alert') {
          <path d="M3 4h14l-5.5 6.5v5l-3 1.5v-6.5L3 4Z" />
        }
        @case ('order') {
          <path d="M4 7h11.5l-3-3M16 13H4.5l3 3" />
        }
        @case ('risk') {
          <path d="M10 2.5 16.5 5v4.5c0 4-3 6.5-6.5 8-3.5-1.5-6.5-4-6.5-8V5L10 2.5Z" />
          <path d="M10 7v3.5M10 13.2v.1" />
        }
        @case ('system') {
          <circle cx="10" cy="10" r="2.5" />
          <path
            d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4"
          />
        }
        @default {
          <circle cx="10" cy="10" r="6.5" />
          <path d="M10 7v3.5M10 13.2v.1" />
        }
      }
    </svg>
  `,
  styles: `
    :host {
      display: inline-grid;
      flex: none;
      place-items: center;
      width: 32px;
      height: 32px;
      border-radius: var(--r-avatar);
      background: var(--sunk);
      color: var(--ink-2);
    }
    :host([data-category='signal']) {
      color: var(--accent);
    }
    :host([data-category='risk']) {
      color: var(--down);
    }
    svg {
      width: 16px;
      height: 16px;
    }
  `,
})
export class CategoryGlyph {
  readonly category = input<string | null>(null);
}
