// Research words for the in-app glossary: the Lab, robustness tests,
// factors, the screener, calendars and options research. Merged into
// `GLOSSARY` by glossary.ts, shown under their own heading on the glossary
// page. Same rules: one plain sentence (two at most), 140 characters or
// fewer, ending with a full stop.

export const RESEARCH_KEYS = [
  // The Lab
  'backtest',
  'lab_run',
  'robustness_tests',
  'lab_verdict',
  'trial_ledger',
  'objective',
  'tuning_share',
  'purged_folds',
  // Robustness tests with no figure of their own elsewhere
  'cpcv',
  'crisis',
  'event_study',
  'vs_random',
  'stress',
  'pool_correlation',
  'signal_ic',
  // Factors
  'factor',
  'tear_sheet',
  'icir',
  'quantile_buckets',
  'warm_up',
  // Screener and universes
  'screener',
  'point_in_time',
  'survivorship_bias',
  'dollar_volume',
  // Calendar and news
  'eps',
  'earnings_surprise',
  'ex_dividend',
  'sentiment_score',
  // Options research
  'implied_volatility',
  'delta',
  'gamma',
  'theta',
  'vega',
  'open_interest',
] as const;

export type ResearchKey = (typeof RESEARCH_KEYS)[number];

export const RESEARCH_GLOSSARY = {
  backtest: {
    term: 'Backtest',
    short:
      'A replay of a strategy over past prices, costs included, to see what it would have done. A first look, not proof.',
    aliases: ['Backtests'],
  },
  lab_run: {
    term: 'Lab run',
    short:
      'A search for good settings on part of the past, then robustness tests on the winner with data the search never saw.',
    aliases: ['Lab runs', 'Test a strategy'],
  },
  robustness_tests: {
    term: 'Robustness tests',
    short:
      'Checks after a backtest, each asking whether the result could be luck or fitted to the past. Also called survival tests.',
    aliases: ['Robustness', 'Robustness test', 'Survival tests', 'Survival test', 'Full robustness tests'],
  },
  lab_verdict: {
    term: 'Lab verdict',
    short:
      'Passed only when every robustness test passed on the best settings. Trials are settings tried, not tests passed.',
    aliases: ['Robustness verdict'],
  },
  trial_ledger: {
    term: 'Trial ledger',
    short:
      'The record of every lab run and every setting it tried. The more tries, the stricter the tests get.',
    aliases: ['Ledger'],
  },
  objective: {
    term: 'Pick the best by',
    short: 'The score that decides which settings win the search, such as Sharpe or CAGR.',
    aliases: ['Objective'],
  },
  tuning_share: {
    term: 'Share used for tuning',
    short:
      'The part of the window the search learns from, such as 0.7. The rest is kept aside to test the winner.',
    aliases: ['Training share', 'Tuning share', 'train_ratio'],
  },
  purged_folds: {
    term: 'Purged folds',
    short:
      'Slices of the past with the days next to each test slice dropped, so the search cannot peek at the test.',
    aliases: ['Purged fold', 'Purged CV'],
  },
  cpcv: {
    term: 'Many splits (CPCV)',
    short:
      'Tests the strategy on many train and test splits it never saw, so one lucky split cannot carry it.',
    aliases: ['CPCV', 'Combinatorial purged CV', 'Many splits'],
  },
  crisis: {
    term: 'Past crises',
    short:
      "Checks the strategy's drop in each past crash it has data for stays within 1.5 times the benchmark's.",
    aliases: ['Crisis periods', 'Crisis'],
  },
  event_study: {
    term: 'After each signal',
    short:
      "Checks that prices really moved the right way after the strategy's entries, beyond their usual drift.",
    aliases: ['Event study'],
  },
  vs_random: {
    term: 'Beats random data',
    short:
      'Runs the same search on random prices shaped like the real ones. The real result must beat what noise finds.',
    aliases: ['Versus random', 'Beats random entries'],
  },
  stress: {
    term: 'Rough conditions',
    short:
      'Simulates many test periods. Even the bad ones must keep a Sharpe above -0.5 and drawdowns within the limit.',
    aliases: ['Stress', 'Stress test'],
  },
  pool_correlation: {
    term: 'Adds something new',
    short: "Checks the strategy's returns do not just copy the strategies already approved.",
    aliases: ['Pool correlation'],
  },
  signal_ic: {
    term: 'Signal IC',
    short:
      "How well a strategy's scores ranked the moves that followed, measured as the IC. For information only.",
    aliases: ['Signal ranking (IC)', 'Signal ranking'],
  },
  factor: {
    term: 'Factor',
    short:
      'A number per company per day that should rank the returns that follow, such as past return or cheapness.',
    aliases: ['Factors'],
  },
  tear_sheet: {
    term: 'Tear sheet',
    short: 'A one-page report of how a factor or strategy did, with its key figures and charts.',
    aliases: ['Tearsheet', 'Tear sheets'],
  },
  icir: {
    term: 'ICIR',
    short:
      'The average IC divided by how much it swings. Above 0.5 means a steady signal, not a few lucky months.',
    aliases: ['IC IR'],
  },
  quantile_buckets: {
    term: 'Buckets',
    short:
      'Groups of names split by factor value, lowest first. A good factor shows returns rising step by step.',
    aliases: ['Quantiles', 'Return per bucket'],
  },
  warm_up: {
    term: 'Warm-up',
    short: 'How many bars of history a factor or strategy needs before its first value.',
    aliases: ['Warmup'],
  },
  screener: {
    term: 'Screener',
    short:
      'Finds instruments by price, trading volume and company numbers, on the data known on a date you pick.',
    aliases: ['Screen', 'Saved screen'],
  },
  point_in_time: {
    term: 'Point in time',
    short:
      'Using only what was known on each past date, so a test never peeks at later prices or restated numbers.',
    aliases: ['Point-in-time'],
  },
  survivorship_bias: {
    term: 'Survivorship bias',
    short:
      'Testing only on names that exist today, which leaves out the failures and makes the past look better.',
    aliases: ['Survivorship'],
  },
  dollar_volume: {
    term: 'Daily dollar volume',
    short:
      'The average money traded in a name each day. Low values mean your trades move the price and cost more.',
    aliases: ['Dollar volume', 'Lowest daily dollar volume', 'Average daily traded value'],
  },
  eps: {
    term: 'EPS',
    short: "Earnings per share: the company's profit for the period divided by its shares.",
    aliases: ['EPS estimate', 'EPS actual', 'Earnings per share'],
  },
  earnings_surprise: {
    term: 'Surprise',
    short: "How far the actual earnings per share came in above or below the analysts' estimate.",
    aliases: ['Earnings surprise'],
  },
  ex_dividend: {
    term: 'Ex-dividend date',
    short:
      'The first day a buyer no longer gets the next dividend. The price often drops by about the dividend.',
    aliases: ['Ex-dividend', 'Ex date'],
  },
  sentiment_score: {
    term: 'Sentiment score',
    short:
      "The data vendor's mood score for news about a ticker, from negative to positive. A rough guide, not a signal.",
    aliases: ['Sentiment', 'Mood'],
  },
  implied_volatility: {
    term: 'Implied volatility (IV)',
    short: "How much movement an option's price expects over a year. Higher means pricier options.",
    aliases: ['IV', 'Call IV', 'Put IV', 'Implied volatility'],
  },
  delta: {
    term: 'Delta',
    short:
      'How much the option price moves when the stock moves 1. Also a rough chance of ending in the money.',
    aliases: ['Call delta', 'Put delta'],
  },
  gamma: {
    term: 'Gamma',
    short: "How fast an option's delta changes as the stock moves.",
    aliases: ['Call gamma', 'Put gamma'],
  },
  theta: {
    term: 'Theta',
    short: 'Money an option loses per share each day as time passes, all else equal.',
    aliases: ['Call theta', 'Put theta'],
  },
  vega: {
    term: 'Vega',
    short: 'Money an option gains or loses per share for one point of change in volatility.',
    aliases: ['Call vega', 'Put vega'],
  },
  open_interest: {
    term: 'Open interest',
    short: 'How many option contracts are open. More open contracts usually mean easier trading.',
    aliases: ['OI', 'Call open interest', 'Put open interest'],
  },
} satisfies Record<ResearchKey, { term: string; short: string; aliases?: readonly string[] }>;
