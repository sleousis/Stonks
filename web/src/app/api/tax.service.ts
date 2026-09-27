import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';
import { toApiError } from '../core/http/api-error';

import { unwrap } from './api-call';
import {
  exportTaxDividends,
  exportTaxGains,
  getFxRate,
  getTaxSettings,
  listTaxLotPicks,
  setTaxLotPicks,
  updateTaxSettings,
} from './generated/sdk.gen';
import type { LotPicksUpdate, TaxSettingsUpdate } from './generated/types.gen';

/** CSV routes need the raw bytes (see ExportsService). */
const AS_BLOB = { responseType: 'blob' } as object;

/**
 * The picked portfolio's base currency and tax settings, its specific-lot
 * picks, the yearly tax CSVs (realized gains, dividends), and the FX rate
 * the system converts with.
 */
@Injectable({ providedIn: 'root' })
export class TaxService {
  private readonly ctx = inject(PortfolioContextService);

  settings() {
    return unwrap(getTaxSettings({ query: this.ctx.query() }));
  }

  updateSettings(body: TaxSettingsUpdate) {
    return unwrap(updateTaxSettings({ query: this.ctx.query(), body }));
  }

  picks(sellFillId?: number) {
    return unwrap(
      listTaxLotPicks({ query: { ...this.ctx.query(), sell_fill_id: sellFillId ?? null } }),
    );
  }

  setPicks(body: LotPicksUpdate) {
    return unwrap(setTaxLotPicks({ query: this.ctx.query(), body }));
  }

  fxRate(base: string, quote: string, day?: string) {
    return unwrap(getFxRate({ query: { base, quote, day: day ?? null } }));
  }

  /** The year's CSV, `gains` or `dividends`, as a blob to save. */
  async download(kind: 'gains' | 'dividends', year: number): Promise<Blob> {
    const query = { ...this.ctx.query(), year };
    const request =
      kind === 'gains'
        ? exportTaxGains({ query, ...AS_BLOB })
        : exportTaxDividends({ query, ...AS_BLOB });
    const result = (await request) as { data?: unknown; error?: unknown; response?: unknown };
    if (result.error !== undefined) {
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
    const data = result.data;
    return data instanceof Blob
      ? data
      : new Blob([typeof data === 'string' ? data : ''], { type: 'text/csv' });
  }
}
