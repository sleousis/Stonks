import { DOCUMENT } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { ShortcutsService } from '../../core/commands/shortcuts.service';
import { Sheet } from './sheet';

interface Row {
  keys: string[];
  /** Pressed one after the other ("g then d") rather than together. */
  sequence?: boolean;
  label: string;
}

/**
 * The "?" cheat sheet: every keyboard shortcut the user may use, and a
 * switch for the single-key ones. Built on `<app-sheet>` like every other
 * dialog (UX-48): a card on larger screens, full screen on phones, Escape
 * closes. Mounted once, in the shell, on first use.
 */
@Component({
  selector: 'app-shortcut-help',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Sheet],
  template: `
    <app-sheet
      [open]="shortcuts.helpOpen()"
      [wide]="true"
      labelledBy="shortcuts-title"
      (dismiss)="close()"
    >
      @if (shortcuts.helpOpen()) {
        <div class="sheet-form">
          <div class="head">
            <h2 id="shortcuts-title">Keyboard shortcuts</h2>
            <button type="button" class="btn btn-ghost" (click)="close()">Close</button>
          </div>
          @for (group of groups(); track group.title) {
            <section [attr.aria-labelledby]="'keys-' + $index">
              <h3 [id]="'keys-' + $index">{{ group.title }}</h3>
              <dl>
                @for (row of group.rows; track row.label) {
                  <div class="row">
                    <dt>
                      @for (k of row.keys; track $index) {
                        @if ($index > 0) {
                          <span class="joiner">{{ row.sequence ? 'then' : '+' }}</span>
                        }
                        <kbd>{{ k }}</kbd>
                      }
                    </dt>
                    <dd>{{ row.label }}</dd>
                  </div>
                }
              </dl>
            </section>
          }
          <label class="check toggle">
            <input
              type="checkbox"
              [checked]="shortcuts.singleKeys()"
              (change)="shortcuts.setSingleKeys($any($event.target).checked)"
            />
            Single-key shortcuts (/, ?, g, n)
          </label>
          <p class="hint">
            Turn these off if you use speech input or they get in the way. {{ mod }}+K always works.
          </p>
        </div>
      }
    </app-sheet>
  `,
  styles: `
    .head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-3);
    }
    h3 {
      margin-bottom: var(--space-1);
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    dl {
      margin: 0;
    }
    .row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-3);
      padding: var(--space-1) 0;
      border-bottom: 1px solid var(--color-border);
    }
    dt {
      display: flex;
      align-items: center;
      gap: var(--space-1);
      flex: none;
    }
    dd {
      margin: 0;
      text-align: end;
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .joiner {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    kbd {
      display: inline-block;
      min-width: 1.8em;
      padding: 1px 6px;
      border: 1px solid var(--color-border-strong);
      border-radius: 3px;
      background: var(--color-surface-2);
      font: inherit;
      font-size: var(--text-xs);
      text-align: center;
    }
    .hint {
      margin-top: calc(-1 * var(--space-3));
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
  `,
})
export class ShortcutHelp {
  protected readonly shortcuts = inject(ShortcutsService);
  private readonly doc = inject(DOCUMENT);

  protected readonly mod = isApple(this.doc) ? '⌘' : 'Ctrl';

  protected readonly groups = computed(() => {
    // Re-read when the cheat sheet opens: the list follows who is signed in.
    this.shortcuts.helpOpen();
    const seqs = this.shortcuts.allSequences;
    const general: Row[] = [
      { keys: [this.mod, 'K'], label: 'Search and run commands' },
      { keys: ['/'], label: 'Search' },
      { keys: ['?'], label: 'Show this list' },
      { keys: ['Esc'], label: 'Close a dialog or the menu' },
    ];
    const toRow = (s: (typeof seqs)[number]): Row => ({
      keys: [s.prefix, s.key],
      sequence: true,
      label: s.label,
    });
    return [
      { title: 'Anywhere', rows: general },
      { title: 'Start something', rows: seqs.filter((s) => s.prefix === 'n').map(toRow) },
      { title: 'Go to a page', rows: seqs.filter((s) => s.prefix === 'g').map(toRow) },
    ].filter((g) => g.rows.length > 0);
  });

  protected close(): void {
    this.shortcuts.helpOpen.set(false);
  }
}

function isApple(doc: Document): boolean {
  const nav = doc.defaultView?.navigator;
  return /Mac|iPhone|iPad/.test(nav?.platform ?? nav?.userAgent ?? '');
}
