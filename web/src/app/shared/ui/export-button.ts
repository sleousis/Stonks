import { DOCUMENT } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject, input, signal } from '@angular/core';

import { type CsvFile, type ExportKind, ExportsService } from '../../api/exports.service';
import { ApiError } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';

/** Save a blob as a file through a temporary link (no new tab, no artifact viewer). */
export function saveFile(doc: Document, file: CsvFile): void {
  const url = URL.createObjectURL(file.blob);
  const link = doc.createElement('a');
  link.href = url;
  link.download = file.filename;
  link.rel = 'noopener';
  doc.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * "Download CSV" for one export. The file comes over the session like any
 * other call, then the browser saves it. A failure toasts the API's reason.
 *
 *   <app-export-button kind="orders" />
 *   <app-export-button kind="lab-trials" [runId]="run.id" label="Download trials" />
 */
@Component({
  selector: 'app-export-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="btn"
      [class.btn-ghost]="ghost()"
      [disabled]="busy()"
      [attr.aria-busy]="busy()"
      (click)="download()"
    >
      <svg class="icon" viewBox="0 0 16 16" aria-hidden="true">
        <path d="M8 2v8m0 0-3-3m3 3 3-3M3 13h10" />
      </svg>
      {{ busy() ? 'Preparing file' : label() }}
    </button>
  `,
  styles: `
    :host {
      display: inline-flex;
    }
    .icon {
      width: 14px;
      height: 14px;
      fill: none;
      stroke: currentColor;
      stroke-width: 1.6;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    button {
      gap: var(--space-2);
    }
  `,
})
export class ExportButton {
  readonly kind = input.required<ExportKind>();
  readonly label = input('Download CSV');
  readonly runId = input<string | null>(null);
  readonly ghost = input(false);

  private readonly exports = inject(ExportsService);
  private readonly toasts = inject(ToastService);
  private readonly doc = inject(DOCUMENT);
  protected readonly busy = signal(false);

  protected async download(): Promise<void> {
    this.busy.set(true);
    try {
      const file = await this.exports.download(this.kind(), { runId: this.runId() ?? undefined });
      saveFile(this.doc, file);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'The file could not be made.';
      this.toasts.error(message, 'Download failed');
    } finally {
      this.busy.set(false);
    }
  }
}
