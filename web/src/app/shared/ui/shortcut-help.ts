import { DOCUMENT } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  computed,
  effect,
  inject,
  viewChild,
} from '@angular/core';

import { ShortcutsService } from '../../core/commands/shortcuts.service';

interface Row {
  keys: string[];
  /** Pressed one after the other ("g then d") rather than together. */
  sequence?: boolean;
  label: string;
}

/**
 * The "?" cheat sheet: every keyboard shortcut, and a switch for the
 * single-key ones. Mounted once, in the shell.
 */
@Component({
  selector: 'app-shortcut-help',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <dialog
      #dialog
      class="sheet"
      aria-labelledby="shortcuts-title"
      (close)="shortcuts.helpOpen.set(false)"
    >
      <div class="head">
        <h2 id="shortcuts-title">Keyboard shortcuts</h2>
        <button type="button" class="btn btn-ghost" (click)="shortcuts.helpOpen.set(false)">
          Close
        </button>
      </div>
      <div class="body">
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
    </dialog>
  `,
  styles: `
    @use 'breakpoints' as bp;

    .sheet {
      width: min(560px, calc(100vw - 32px));
      max-height: calc(100dvh - 48px);
      padding: 0;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-lg);
      background: var(--color-surface);
      color: var(--color-ink);
      box-shadow: var(--shadow-2);
    }
    .sheet::backdrop {
      background: var(--color-scrim);
    }
    .head {
      position: sticky;
      top: 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-3);
      padding: var(--space-3) var(--space-3) var(--space-3) var(--space-5);
      border-bottom: 1px solid var(--color-border);
      background: var(--color-surface);
    }
    h2 {
      font-size: var(--text-lg);
    }
    .body {
      display: grid;
      gap: var(--space-4);
      padding: var(--space-4) var(--space-5) var(--space-5);
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
    @include bp.phone {
      .sheet {
        width: 100vw;
        max-width: 100vw;
        height: 100dvh;
        max-height: 100dvh;
        margin: 0;
        border: 0;
        border-radius: 0;
      }
      .head {
        padding-top: calc(var(--space-3) + env(safe-area-inset-top));
      }
      .body {
        padding-inline: var(--space-4);
        padding-bottom: calc(var(--space-5) + env(safe-area-inset-bottom));
      }
    }
  `,
})
export class ShortcutHelp {
  protected readonly shortcuts = inject(ShortcutsService);
  private readonly doc = inject(DOCUMENT);
  private readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>('dialog');

  protected readonly mod = isApple(this.doc) ? '⌘' : 'Ctrl';

  protected readonly groups = computed(() => {
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

  constructor() {
    effect(() => {
      const el = this.dialog().nativeElement;
      if (this.shortcuts.helpOpen()) {
        if (!el.open) el.showModal?.();
      } else if (el.open) {
        el.close();
      }
    });
  }
}

function isApple(doc: Document): boolean {
  const nav = doc.defaultView?.navigator;
  return /Mac|iPhone|iPad/.test(nav?.platform ?? nav?.userAgent ?? '');
}
