import type { OptionsLiveView } from '../app/api/models';

/** Options live off for a Broker paper portfolio, with every reason listed. */
export const OPTIONS_OFF: OptionsLiveView = {
  portfolio_id: 'pf_live',
  enabled: false,
  stage: 'broker_paper',
  level: 'none',
  allowed: false,
  reasons: [
    'options live is off: the admin has not turned it on',
    'the portfolio is at stage broker_paper, not live_small or higher',
    'the options approval level is none',
  ],
  reason: null,
  updated_at: null,
  updated_by: null,
  levels: [
    { level: 'none', allows: 'No option order opens. Closes still go out.' },
    { level: 'covered', allows: 'Covered calls, cash-secured puts.' },
    { level: 'spreads', allows: 'Everything in covered, plus verticals.' },
    { level: 'naked', allows: 'Everything in spreads, plus uncovered short puts.' },
  ],
  auto_approve_closes: false,
  expiry_action: 'close',
  close_sessions: 1,
};
