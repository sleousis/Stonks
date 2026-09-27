import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';
import { toApiError } from '../core/http/api-error';
import {
  exportFills,
  exportJournal,
  exportLabTrials,
  exportOrders,
  exportPnl,
  exportSnapshots,
} from './generated/sdk.gen';

export type ExportKind = 'orders' | 'fills' | 'journal' | 'snapshots' | 'pnl' | 'lab-trials';

/**
 * The generated client asks Angular for JSON by default. CSV routes need the
 * raw bytes, so each call passes `responseType: 'blob'`, which the client
 * hands to `HttpRequest` untouched.
 */
const AS_BLOB = { responseType: 'blob' } as object;

/** A CSV file ready to save. */
export interface CsvFile {
  blob: Blob;
  filename: string;
}

/**
 * CSV downloads of the picked portfolio (orders, fills, journal, snapshots,
 * P&L) and of the lab's trial results. They ride on the session like any
 * other call, then the page saves the blob.
 */
@Injectable({ providedIn: 'root' })
export class ExportsService {
  private readonly ctx = inject(PortfolioContextService);

  async download(kind: ExportKind, options: { runId?: string; since?: string } = {}) {
    const portfolio = this.ctx.query();
    let data: unknown;
    switch (kind) {
      case 'orders':
        data = await unwrapBlob(exportOrders({ query: portfolio, ...AS_BLOB }));
        break;
      case 'fills':
        data = await unwrapBlob(exportFills({ query: portfolio, ...AS_BLOB }));
        break;
      case 'journal':
        data = await unwrapBlob(exportJournal({ query: portfolio, ...AS_BLOB }));
        break;
      case 'snapshots':
        data = await unwrapBlob(exportSnapshots({ query: portfolio, ...AS_BLOB }));
        break;
      case 'pnl':
        data = await unwrapBlob(
          exportPnl({ query: { ...portfolio, since: options.since ?? null }, ...AS_BLOB }),
        );
        break;
      case 'lab-trials':
        data = await unwrapBlob(
          exportLabTrials({ query: { run_id: options.runId ?? null }, ...AS_BLOB }),
        );
        break;
    }
    const scope =
      kind === 'lab-trials' ? (options.runId ?? 'all') : (portfolio.portfolio_id ?? 'mine');
    return { blob: asBlob(data), filename: csvName(kind, scope) } satisfies CsvFile;
  }
}

/**
 * Like `unwrap`, but a failed blob request carries its problem details as a
 * Blob: read it back as JSON so the error says what went wrong.
 */
async function unwrapBlob(
  request: Promise<{ data?: unknown; error?: unknown; response?: unknown }>,
): Promise<unknown> {
  const result = await request;
  if (result.error === undefined) return result.data;
  let body = result.error;
  if (body instanceof Blob) {
    const text = await body.text();
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  throw toApiError(body, result.response);
}

function asBlob(data: unknown): Blob {
  if (data instanceof Blob) return data;
  return new Blob([typeof data === 'string' ? data : ''], { type: 'text/csv' });
}

/** `stonks-orders-mine-2026-09-27.csv`, like the server's own name. */
export function csvName(kind: ExportKind, scope: string, today = new Date()): string {
  const safe = scope.replace(/[^A-Za-z0-9_-]/g, '-').slice(0, 64) || 'all';
  const day = today.toISOString().slice(0, 10);
  return `stonks-${kind}-${safe}-${day}.csv`;
}
