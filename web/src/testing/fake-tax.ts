import type { Provider } from '@angular/core';

import type { TaxPreviewView } from '../app/api/models';
import { TaxService } from '../app/api/tax.service';

/** A tax preview of a trade that closes lots in `lots`, gain `gain`. */
export function taxPreview(over: Partial<TaxPreviewView> = {}): TaxPreviewView {
  return {
    portfolio_id: 'pf_1',
    ticker: 'AAA.US',
    side: 'sell',
    quantity: 10,
    price: 25,
    currency: 'USD',
    jurisdiction: 'us',
    lot_method: 'fifo',
    lots: [],
    proceeds: 0,
    realized_gain: 0,
    short_term_gain: 0,
    long_term_gain: 0,
    wash_sale_disallowed: 0,
    estimated_tax: 0,
    after_tax_proceeds: 0,
    year_tax_change: 0,
    wash_sale_warning: null,
    rates: { short_term: 0.32, long_term: 0.15 },
    ...over,
  };
}

/**
 * A quiet TaxService for specs of pages that embed the tax preview (the
 * order ticket, the drafts) but test something else: nothing to show, no HTTP.
 */
export function provideFakeTax(overrides: Partial<TaxService> = {}): Provider {
  return {
    provide: TaxService,
    useValue: {
      preview: () => Promise.resolve(taxPreview()),
      ...overrides,
    },
  };
}
