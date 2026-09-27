import { ChangeDetectionStrategy, Component, DestroyRef, inject, input, signal } from '@angular/core';

/** Put text on the clipboard. False when the browser blocks it (plain http, policy). */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard?.writeText) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Save text as a file on the viewer's device (recovery codes, a new password). */
export function downloadText(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * A Copy button for one value. Says "Copied" for a moment, or, when the
 * browser blocks the clipboard, says so in a line under the button.
 */
@Component({
  selector: 'app-copy-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="btn"
      [attr.aria-label]="label() ? 'Copy ' + label() : null"
      (click)="copy()"
    >
      {{ copied() ? 'Copied' : 'Copy' }}
    </button>
    <span class="visually-hidden" role="status">{{ copied() ? (label() || 'Text') + ' copied' : '' }}</span>
    @if (blocked()) {
      <span class="blocked" role="alert">{{ blockedText() }}</span>
    }
  `,
  styles: `
    :host {
      display: inline-grid;
      gap: var(--space-1);
      justify-items: start;
    }
    .blocked {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
  `,
})
export class CopyButton {
  readonly text = input.required<string>();
  /** What is copied, for screen readers ("key", "password"). */
  readonly label = input('');
  readonly blockedText = input('Copy is blocked here. Select the text and copy it.');

  protected readonly copied = signal(false);
  protected readonly blocked = signal(false);
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      if (this.timer) clearTimeout(this.timer);
    });
  }

  protected async copy(): Promise<void> {
    const ok = await copyText(this.text());
    this.blocked.set(!ok);
    this.copied.set(ok);
    if (ok) {
      if (this.timer) clearTimeout(this.timer);
      this.timer = setTimeout(() => this.copied.set(false), 2500);
    }
  }
}
