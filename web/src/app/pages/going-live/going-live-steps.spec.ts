import type {
  ConnectionView,
  GatewayView,
  LiveStageView,
  ProviderView,
  SubscriptionView,
} from '../../api/models';
import type { PortfolioRef } from '../../api/portfolios.service';
import { book } from '../../../testing/portfolio-fixtures';
import { type GoingLiveFacts, atBroker, goingLiveSteps } from './going-live-steps';

const IBKR: ProviderView = {
  name: 'ibkr',
  display_name: 'Interactive Brokers',
  auth_flow: 'api_key',
  capabilities: ['read_positions', 'trade'],
  credential_fields: [],
  can_trade: true,
  enabled: true,
  has_paper: true,
};

const SNAP: ProviderView = {
  ...IBKR,
  name: 'snaptrade',
  display_name: 'SnapTrade',
  can_trade: false,
};

function conn(over: Partial<ConnectionView> = {}): ConnectionView {
  return {
    id: 'con_1',
    provider: 'ibkr',
    label: null,
    status: 'active',
    last_sync_at: null,
    last_sync_status: null,
    last_error: null,
    consecutive_failures: 0,
    next_sync_at: null,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    ...over,
  };
}

function gateway(over: Partial<GatewayView> = {}): GatewayView {
  return {
    gateway: 'paper',
    mode: 'paper',
    connected: true,
    checked: true,
    last_check_at: null,
    last_ok_at: null,
    down_since: null,
    consecutive_failures: 0,
    fault: null,
    detail: null,
    latency_ms: null,
    paused_at: null,
    your_portfolios: ['Sam at IBKR'],
    paused_books: [],
    paused_elsewhere: 0,
    ...over,
  };
}

const STAGE: LiveStageView = {
  portfolio_id: 'pf_b',
  stage: 'broker_paper',
  next_stage: 'live_small',
  real_money: false,
  history: [],
  days: [],
};

function follow(over: Partial<SubscriptionView> = {}): SubscriptionView {
  return {
    id: 'sub_1',
    strategy_id: 'momentum_1a2b3c4d',
    portfolio_id: 'pf_b',
    mode: 'paper',
    enabled: true,
    weight: 1,
    paper_days_completed: 20,
    paper_days_required: 20,
    auto_blockers: [],
    auto_enabled_at: null,
    paused_reason: null,
    strategy_status: 'active',
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    ...over,
  } as SubscriptionView;
}

const BROKER = book({
  id: 'pf_b',
  name: 'Sam at IBKR',
  kind: 'broker',
  trading: 'live',
  broker_connection_id: 'con_1',
});

function facts(over: Partial<GoingLiveFacts> = {}): GoingLiveFacts {
  return {
    portfolio: BROKER,
    isAdmin: false,
    gateways: { configured: true, gateways: [gateway()] },
    connections: [conn()],
    providers: [IBKR, SNAP],
    stage: STAGE,
    report: {
      portfolio_id: 'pf_b',
      from_stage: 'broker_paper',
      target: 'live_small',
      passed: false,
      checks: [
        { name: 'allocation_set', passed: false, detail: '' },
        { name: 'clean_sessions', passed: true, detail: '' },
      ],
      metrics: {},
      computed_at: '2026-09-27T20:00:00Z',
    },
    allocation: {
      portfolio_id: 'pf_b',
      amount: null,
      currency: null,
      reason: null,
      updated_at: null,
      updated_by: null,
    },
    profile: null,
    rules: {
      portfolio_id: 'pf_b',
      safeguards: [
        { name: 'capital_ramp', on: true, settings: {} },
        { name: 'price_band', on: false, settings: {} },
      ],
      account_rules_on: true,
      profile_set: false,
      account_rules: [],
    },
    follows: [follow()],
    previewedAt: null,
    ...over,
  };
}

const byKey = (f: GoingLiveFacts) => Object.fromEntries(goingLiveSteps(f).map((s) => [s.key, s]));

describe('goingLiveSteps', () => {
  it('walks the eight steps in order', () => {
    expect(goingLiveSteps(facts()).map((s) => s.key)).toEqual([
      'gateway',
      'broker',
      'stage',
      'allocation',
      'profile',
      'safeguards',
      'preview',
      'mode',
    ]);
  });

  it('says what is done and what is not, with a link to each', () => {
    const s = byKey(facts());
    expect(s['gateway'].state).toBe('done');
    expect(s['broker'].state).toBe('done');
    expect(s['broker'].detail).toContain('Interactive Brokers');
    expect(s['stage'].state).toBe('todo');
    expect(s['stage'].detail).toContain('At Broker paper. Next is Real money, small');
    expect(s['stage'].detail).toContain('Still needed: Allocation set.');
    expect(s['allocation']).toMatchObject({
      state: 'todo',
      link: { path: '/profile/live/pf_b', fragment: 'allocation' },
    });
    expect(s['profile'].state).toBe('todo');
    expect(s['safeguards']).toMatchObject({ state: 'admin' });
    expect(s['safeguards'].detail).toContain('1 of 2 are on');
    expect(s['preview'].state).toBe('todo');
    expect(s['mode'].state).toBe('todo');
    expect(s['mode'].detail).toContain('Alerts only or Paper');
  });

  it('marks everything done on a portfolio at a Real money stage', () => {
    const s = byKey(
      facts({
        stage: { ...STAGE, stage: 'live_small', real_money: true },
        allocation: { ...facts().allocation!, amount: 2500, currency: 'USD' },
        profile: { portfolio_id: 'pf_b', jurisdiction: 'us' },
        rules: {
          ...facts().rules!,
          safeguards: [{ name: 'capital_ramp', on: true, settings: {} }],
        },
        follows: [follow({ mode: 'approve' })],
        previewedAt: '2026-09-27T10:00:00Z',
      }),
    );
    expect(Object.values(s).every((step) => step.state === 'done')).toBe(true);
    expect(s['stage'].detail).toContain('At Real money, small');
    expect(s['allocation'].detail).toContain('$2,500');
    expect(s['mode'].detail).toContain('1 follow here uses Approve each trade or Automatic');
  });

  it('names who acts on the server gateway', () => {
    const none = byKey(facts({ gateways: { configured: false, gateways: [] } }));
    expect(none['gateway']).toMatchObject({ state: 'admin', link: null });
    expect(none['gateway'].detail).toContain('Your admin sets up');
    const admin = byKey(facts({ isAdmin: true, gateways: { configured: false, gateways: [] } }));
    expect(admin['gateway'].link).toMatchObject({ path: '/health' });
    const down = byKey(
      facts({ gateways: { configured: true, gateways: [gateway({ connected: false })] } }),
    );
    expect(down['gateway'].detail).toContain('not connected');
    const other = byKey(
      facts({ gateways: { configured: true, gateways: [gateway({ your_portfolios: [] })] } }),
    );
    expect(other['gateway'].detail).toContain('not for this portfolio');
  });

  it('refuses a read-only broker as the link to real money', () => {
    const s = byKey(facts({ connections: [conn({ provider: 'snaptrade' })] }));
    expect(s['broker'].state).toBe('todo');
    expect(s['broker'].detail).toContain('SnapTrade only reads');
  });

  it('holds the later steps of a paper portfolio behind the broker step', () => {
    const s = byKey(facts({ portfolio: book({ id: 'pf_p', name: 'Paper' }) }));
    expect(s['broker'].state).toBe('todo');
    expect(s['broker'].link).toMatchObject({ path: '/connections' });
    for (const key of ['stage', 'allocation', 'profile', 'safeguards', 'preview']) {
      expect(s[key].state).toBe('blocked');
    }
  });

  it('checks while the facts load', () => {
    const s = byKey(facts({ stage: undefined, allocation: undefined, follows: undefined }));
    expect(s['stage'].state).toBe('checking');
    expect(s['allocation'].state).toBe('checking');
    expect(s['mode'].state).toBe('checking');
  });

  it('sends a portfolio with no follows to the strategies first', () => {
    const s = byKey(facts({ follows: [] }));
    expect(s['mode'].link).toMatchObject({ path: '/strategies' });
  });
});

describe('atBroker', () => {
  it('counts a broker portfolio at every stage, not only at real money', () => {
    const paper: PortfolioRef = { ...BROKER, trading: 'paper', live_stage: 'broker_paper' };
    expect(atBroker(paper)).toBe(true);
    expect(atBroker(BROKER)).toBe(true);
    expect(atBroker(book({ id: 'pf_s', name: 'Sim' }))).toBe(false);
  });
});
