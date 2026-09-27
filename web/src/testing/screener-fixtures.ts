import type { MetricView, SavedScreenView, ScreenResult } from '../app/api/models';

/** A few screen metrics, one of each unit. */
export const METRICS: MetricView[] = [
  { id: 'price', label: 'Price', group: 'price', unit: 'money', description: 'Last close.' },
  {
    id: 'return_12m',
    label: '12-month return',
    group: 'price',
    unit: 'percent',
    description: 'Adjusted return over a year.',
  },
  {
    id: 'pe_ratio',
    label: 'P/E',
    group: 'fundamental',
    unit: 'ratio',
    description: 'Price over trailing earnings.',
  },
  {
    id: 'dividend_yield',
    label: 'Dividend yield',
    group: 'fundamental',
    unit: 'percent',
    description: 'Dividends over the last close.',
  },
];

export const SAVED: SavedScreenView = {
  id: 'scr_1',
  name: 'Cheap payers',
  spec: { filters: [{ metric: 'dividend_yield', min: 0.04, max: null }], limit: 50 },
  created_at: '2026-09-20T10:00:00Z',
  updated_at: '2026-09-21T10:00:00Z',
};

export const RESULT: ScreenResult = {
  as_of: '2026-09-25',
  candidates: 120,
  matched: 2,
  metrics: ['dividend_yield', 'price'],
  truncated: false,
  rows: [
    {
      ticker: 'KO.US',
      name: 'Coca-Cola',
      sector: 'Consumer Staples',
      exchange: 'US',
      values: { dividend_yield: 0.031, price: 62.5 },
    },
    {
      ticker: 'T.US',
      name: 'AT&T',
      sector: 'Communication',
      exchange: 'US',
      values: { dividend_yield: 0.065, price: null },
    },
  ],
};
