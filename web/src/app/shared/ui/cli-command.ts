import { ChangeDetectionStrategy, Component, input, signal } from '@angular/core';

/**
 * A shell command shown as code with a copy button. Use it where the console
 * cannot do something yet and the CLI can.
 *
 *   <app-cli-command command="stonks golive check momentum-v3" />
 */
@Component({
  selector: 'app-cli-command',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="cli">
      <code class="command"><span class="prompt" aria-hidden="true">$ </span>{{ command() }}</code>
      <button
        type="button"
        class="btn btn-ghost copy"
        [attr.aria-label]="copied() ? 'Copied' : 'Copy command'"
        (click)="copy()"
      >
        {{ copied() ? 'Copied' : 'Copy' }}
      </button>
    </div>
  `,
  styles: `
    .cli {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      min-width: 0;
      padding: var(--space-1) var(--space-1) var(--space-1) var(--space-3);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
    }
    .command {
      flex: 1 1 auto;
      min-width: 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .prompt {
      color: var(--color-ink-3);
      user-select: none;
    }
    .copy {
      flex: none;
    }
  `,
})
export class CliCommand {
  readonly command = input.required<string>();
  protected readonly copied = signal(false);

  protected async copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.command());
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 2000);
    } catch {
      // Clipboard blocked (insecure origin or permissions): the text stays selectable.
    }
  }
}
