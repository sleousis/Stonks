import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { TabWorkspace } from './nav-items';

/**
 * The line icon of a workspace (1.6 stroke, like every console icon).
 * Decorative: the workspace name beside it carries the words.
 */
@Component({
  selector: 'app-workspace-icon',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { 'aria-hidden': 'true' },
  template: `
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6">
      @switch (name()) {
        @case ('Trade') {
          <path d="M3 14.5 7.5 10l3 3L17 6.5" stroke-linecap="round" stroke-linejoin="round" />
          <path d="M12.5 6.5H17V11" stroke-linecap="round" stroke-linejoin="round" />
        }
        @case ('Research') {
          <circle cx="8.5" cy="8.5" r="5" />
          <path d="m12.5 12.5 4 4" stroke-linecap="round" />
        }
        @case ('Portfolio') {
          <rect x="2.5" y="5" width="15" height="11" rx="2" />
          <path d="M2.5 8.5h15M13 12h1.5" stroke-linecap="round" />
        }
        @case ('System') {
          <circle cx="10" cy="10" r="2.5" />
          <path
            d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4"
            stroke-linecap="round"
          />
        }
        @default {
          <rect x="3" y="3" width="5.5" height="5.5" rx="1.2" />
          <rect x="11.5" y="3" width="5.5" height="5.5" rx="1.2" />
          <rect x="3" y="11.5" width="5.5" height="5.5" rx="1.2" />
          <rect x="11.5" y="11.5" width="5.5" height="5.5" rx="1.2" />
        }
      }
    </svg>
  `,
  styles: `
    :host {
      display: inline-grid;
      place-items: center;
      flex: none;
    }
    svg {
      width: 100%;
      height: 100%;
    }
  `,
})
export class WorkspaceIcon {
  readonly name = input<TabWorkspace | 'all'>('all');
}
