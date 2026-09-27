import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { copyText, downloadText } from './copy-button';

/**
 * A secret shown once (recovery codes, a new API token) with a Copy button.
 * The value is never stored; it lives only as long as this component.
 */
@Component({
  selector: 'app-one-time-secret',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="box">
      <p class="warn">{{ warning() }}</p>
      @if (values().length === 1) {
        <code class="single">{{ values()[0] }}</code>
      } @else {
        <ol class="codes" [attr.aria-label]="label()">
          @for (v of values(); track v) {
            <li>
              <code>{{ v }}</code>
            </li>
          }
        </ol>
      }
      <div class="actions">
        <button type="button" class="btn" (click)="copy()">
          {{ copied() ? 'Copied' : 'Copy' }}
        </button>
        @if (filename(); as name) {
          <button type="button" class="btn" (click)="download(name)">Download .txt</button>
        }
        <span class="visually-hidden" role="status">{{ copied() ? label() + ' copied' : '' }}</span>
      </div>
      @if (blocked()) {
        <p class="blocked" role="alert">
          Copy is blocked here. Select and copy, or write them down.
        </p>
      }
    </div>
  `,
  styles: `
    .box {
      display: grid;
      gap: var(--space-3);
      padding: var(--space-4);
      border: 1px solid var(--color-border-strong);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-md);
      background: var(--color-warn-soft);
      min-width: 0;
    }
    .warn {
      font-size: var(--text-sm);
      font-weight: var(--weight-semibold);
    }
    .single {
      display: block;
      overflow-wrap: anywhere;
      padding: var(--space-2) var(--space-3);
      border-radius: var(--radius-sm);
      background: var(--color-surface);
      font-size: var(--text-md);
      user-select: all;
    }
    .codes {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(9rem, 1fr));
      gap: var(--space-2);
      margin: 0;
      padding-left: var(--space-5);
    }
    .codes code {
      font-size: var(--text-md);
      font-variant-numeric: tabular-nums;
      user-select: all;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .blocked {
      font-size: var(--text-sm);
    }
  `,
})
export class OneTimeSecret {
  readonly values = input.required<readonly string[]>();
  readonly label = input('Secret');
  readonly warning = input('Shown once. Save it now.');
  /** Offer "Download .txt" under this file name (recovery codes). */
  readonly filename = input<string | null>(null);

  protected readonly copied = signal(false);
  /** The browser refused the clipboard (plain http, policy): say so (UX-50). */
  protected readonly blocked = signal(false);
  private readonly text = computed(() => this.values().join('\n'));

  protected async copy(): Promise<void> {
    const ok = await copyText(this.text());
    this.blocked.set(!ok);
    if (!ok) return;
    this.copied.set(true);
    setTimeout(() => this.copied.set(false), 2500);
  }

  protected download(name: string): void {
    downloadText(name, `${this.text()}\n`);
  }
}
