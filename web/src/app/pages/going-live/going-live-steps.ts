import type {
  AccountProfileView,
  ConnectionView,
  GateReportView,
  GatewayHealthView,
  LiveAllocationView,
  LiveRulesView,
  LiveStageView,
  ProviderView,
  SubscriptionView,
} from '../../api/models';
import type { PortfolioRef } from '../../api/portfolios.service';
import { formatDate, formatMoney } from '../../core/format/format';
import { checkLabel, stageWords } from '../../shared/live-stages';

/**
 * Done, still yours to do, waiting for your admin, or waiting on an earlier
 * step. `checking` while its facts load.
 */
export type StepState = 'done' | 'todo' | 'admin' | 'blocked' | 'checking';

export interface StepLink {
  label: string;
  /** Router path segments. */
  path: string;
  fragment?: string;
  query?: Record<string, string>;
}

export interface GoingLiveStep {
  key:
    'gateway' | 'broker' | 'stage' | 'allocation' | 'profile' | 'safeguards' | 'preview' | 'mode';
  title: string;
  state: StepState;
  /** One plain sentence: what is true now, or what to do. */
  detail: string;
  link: StepLink | null;
}

/** What the checklist knows about one portfolio. `undefined`: still loading. */
export interface GoingLiveFacts {
  portfolio: PortfolioRef;
  isAdmin: boolean;
  gateways: GatewayHealthView | undefined;
  connections: readonly ConnectionView[] | undefined;
  providers: readonly ProviderView[] | undefined;
  stage: LiveStageView | undefined;
  report: GateReportView | undefined;
  allocation: LiveAllocationView | undefined;
  /** `null`: no profile saved yet. */
  profile: AccountProfileView | null | undefined;
  rules: LiveRulesView | undefined;
  follows: readonly SubscriptionView[] | undefined;
  /** When a preview last ran on this browser, or null. */
  previewedAt: string | null;
}

export const STATE_WORDS: Record<StepState, string> = {
  done: 'Done',
  todo: 'Not yet',
  admin: 'Waiting for your admin',
  blocked: 'Needs an earlier step',
  checking: 'Checking',
};

const checking = (key: GoingLiveStep['key'], title: string): GoingLiveStep => ({
  key,
  title,
  state: 'checking',
  detail: 'Checking…',
  link: null,
});

/** A portfolio linked to a real broker: the only kind that can reach real money. */
export function atBroker(p: PortfolioRef): boolean {
  return p.trading === 'live';
}

/**
 * The whole path to real money for one portfolio, in order (F52): the
 * server's broker gateway, the broker connection, the portfolio's stage,
 * the allocation, the account profile, the safeguards, a dry-run preview,
 * and a follow switched to Approve each trade or Automatic. Each step says
 * whether it is done, who acts, and where.
 */
export function goingLiveSteps(f: GoingLiveFacts): GoingLiveStep[] {
  const p = f.portfolio;
  const broker = atBroker(p);
  const settings = (fragment: string, label: string): StepLink => ({
    label,
    path: `/profile/live/${p.id}`,
    fragment,
  });
  const needsBroker = (key: GoingLiveStep['key'], title: string): GoingLiveStep => ({
    key,
    title,
    state: 'blocked',
    detail: 'Link this portfolio to a broker account first (step 2).',
    link: null,
  });

  return [
    gatewayStep(f),
    brokerStep(f),
    !broker
      ? needsBroker('stage', 'Portfolio stage')
      : stageStep(f, settings('stage', 'Open the stage')),
    !broker
      ? needsBroker('allocation', 'Allocation')
      : f.allocation === undefined
        ? checking('allocation', 'Allocation')
        : f.allocation.amount == null
          ? {
              key: 'allocation',
              title: 'Allocation',
              state: 'todo',
              detail: 'Set the most Stonks may hold in this account. Nothing opens until you do.',
              link: settings('allocation', 'Set the allocation'),
            }
          : {
              key: 'allocation',
              title: 'Allocation',
              state: 'done',
              detail: `Set to ${formatMoney(f.allocation.amount, { currency: f.allocation.currency ?? p.base_currency })}. Only you change it.`,
              link: settings('allocation', 'Change it'),
            },
    !broker
      ? needsBroker('profile', 'Account profile')
      : f.profile === undefined
        ? checking('profile', 'Account profile')
        : f.profile === null
          ? {
              key: 'profile',
              title: 'Account profile',
              state: 'todo',
              detail: 'Say where the broker holds the account and whether it is cash or margin.',
              link: settings('account-profile', 'Save the profile'),
            }
          : {
              key: 'profile',
              title: 'Account profile',
              state: 'done',
              detail: 'Saved. It picks the account rules for this account.',
              link: settings('account-profile', 'Review it'),
            },
    !broker ? needsBroker('safeguards', 'Safeguards') : safeguardsStep(f, settings),
    !broker
      ? needsBroker('preview', 'Preview the next orders')
      : f.previewedAt
        ? {
            key: 'preview',
            title: 'Preview the next orders',
            state: 'done',
            detail: `Previewed on ${formatDate(f.previewedAt)} on this device. Nothing was sent.`,
            link: settings('preview', 'Preview again'),
          }
        : {
            key: 'preview',
            title: 'Preview the next orders',
            state: 'todo',
            detail: "Run a dry run through the broker's own check. It never sends an order.",
            link: settings('preview', 'Preview now'),
          },
    modeStep(f),
  ];
}

function gatewayStep(f: GoingLiveFacts): GoingLiveStep {
  const title = 'Server gateway set up';
  if (f.gateways === undefined) return checking('gateway', title);
  const health = f.isAdmin ? { label: 'Open Health', path: '/health' } : null;
  if (!f.gateways.configured || f.gateways.gateways.length === 0) {
    return {
      key: 'gateway',
      title,
      state: 'admin',
      detail: f.isAdmin
        ? 'Set up an IB Gateway on the server and list this portfolio for it.'
        : 'Your admin sets up an IB Gateway on the server for this portfolio.',
      link: health,
    };
  }
  const mine = f.gateways.gateways.find((g) => g.your_portfolios.includes(f.portfolio.name));
  if (!mine) {
    return {
      key: 'gateway',
      title,
      state: 'admin',
      detail: 'A gateway runs on the server, but not for this portfolio yet. Your admin adds it.',
      link: health,
    };
  }
  const kind = mine.mode === 'live' ? 'real-money' : 'paper';
  if (!mine.connected) {
    return {
      key: 'gateway',
      title,
      state: 'admin',
      detail: `The ${kind} gateway for this portfolio is not connected right now.`,
      link: health,
    };
  }
  return {
    key: 'gateway',
    title,
    state: 'done',
    detail: `The ${kind} gateway for this portfolio is connected.`,
    link: health,
  };
}

function brokerStep(f: GoingLiveFacts): GoingLiveStep {
  const title = 'Broker connected';
  const p = f.portfolio;
  const connect: StepLink = { label: 'Open Broker connections', path: '/connections' };
  if (!atBroker(p)) {
    return {
      key: 'broker',
      title,
      state: 'todo',
      detail:
        'Connect a broker that can trade, such as Interactive Brokers, and link its account to a portfolio.',
      link: connect,
    };
  }
  if (!p.broker_connection_id) {
    // The server's own broker book (no connection row): the gateway is the link.
    return {
      key: 'broker',
      title,
      state: 'done',
      detail: 'This portfolio trades through the broker the server is set up for.',
      link: null,
    };
  }
  if (f.connections === undefined || f.providers === undefined) return checking('broker', title);
  const link: StepLink = {
    label: 'Open the connection',
    path: `/connections/${p.broker_connection_id}`,
  };
  const conn = f.connections.find((c) => c.id === p.broker_connection_id);
  const provider = conn ? f.providers.find((x) => x.name === conn.provider) : undefined;
  if (!conn || conn.status !== 'active') {
    return {
      key: 'broker',
      title,
      state: 'todo',
      detail: 'The connection needs attention before Stonks can use it.',
      link: conn ? link : connect,
    };
  }
  if (!provider?.can_trade) {
    return {
      key: 'broker',
      title,
      state: 'todo',
      detail: `${provider?.display_name ?? 'This broker'} only reads. Link an account at a broker that can trade.`,
      link: connect,
    };
  }
  return {
    key: 'broker',
    title,
    state: 'done',
    detail: `Linked to ${provider.display_name}, which can take orders when you allow it.`,
    link,
  };
}

function stageStep(f: GoingLiveFacts, link: StepLink): GoingLiveStep {
  const title = 'Portfolio stage';
  if (f.stage === undefined) return checking('stage', title);
  const now = stageWords(f.stage.stage);
  if (f.stage.real_money) {
    return {
      key: 'stage',
      title,
      state: 'done',
      detail: `At ${now.label}: real money moves, within your allocation.`,
      link,
    };
  }
  const next = f.report?.target ? stageWords(f.report.target).label : null;
  const missing = (f.report?.checks ?? [])
    .filter((c) => c.passed === false)
    .map((c) => checkLabel(c.name));
  const still = missing.length ? ` Still needed: ${missing.join(', ')}.` : '';
  return {
    key: 'stage',
    title,
    state: 'todo',
    detail: next
      ? `At ${now.label}. Next is ${next}, one stage at a time.${still}`
      : `At ${now.label}.${still}`,
    link,
  };
}

function safeguardsStep(
  f: GoingLiveFacts,
  settings: (fragment: string, label: string) => StepLink,
): GoingLiveStep {
  const title = 'Safeguards';
  if (f.rules === undefined) return checking('safeguards', title);
  const all = f.rules.safeguards;
  const on = all.filter((r) => r.on).length;
  const link = settings('safeguards', 'See the safeguards');
  if (all.length > 0 && on === all.length) {
    return { key: 'safeguards', title, state: 'done', detail: `All ${on} are on.`, link };
  }
  return {
    key: 'safeguards',
    title,
    state: 'admin',
    detail: `${on} of ${all.length} are on. Your admin turns them on; your own limits can only make them stricter.`,
    link,
  };
}

function modeStep(f: GoingLiveFacts): GoingLiveStep {
  const title = 'Switch a follow to Approve each trade or Automatic';
  if (f.follows === undefined) return checking('mode', title);
  const here = f.follows.filter((s) => s.portfolio_id === f.portfolio.id && s.enabled);
  const trading = here.filter((s) => s.mode === 'approve' || s.mode === 'auto');
  const today: StepLink = { label: 'Open Today', path: '/' };
  if (trading.length) {
    const n = trading.length;
    return {
      key: 'mode',
      title,
      state: 'done',
      detail: `${n} follow${n === 1 ? '' : 's'} here ${n === 1 ? 'uses' : 'use'} Approve each trade or Automatic. Orders go out at this portfolio's stage.`,
      link: today,
    };
  }
  if (!here.length) {
    return {
      key: 'mode',
      title,
      state: 'todo',
      detail: 'Follow an approved strategy in this portfolio first, on Paper.',
      link: { label: 'Browse strategies', path: '/strategies' },
    };
  }
  return {
    key: 'mode',
    title,
    state: 'todo',
    detail:
      'Your follows here are Alerts only or Paper. Switch one on Today. Automatic opens after enough paper days.',
    link: today,
  };
}
