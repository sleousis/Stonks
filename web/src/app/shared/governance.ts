import type { BrokerInfo, GoLiveReport, StatusChangeRequest } from '../api/models';
import { ApiError, errorMessage } from '../core/http/api-error';
import type { ToastService } from '../core/notify/toast.service';
import { LIFECYCLE } from './governance-labels';
import {
  OVERRIDE_MIN_REASON,
  type StatusChangeDialog,
  type StatusChangeOptions,
  type StatusChangeTicket,
} from './ui/status-change-dialog';

/** The go-live check refused the approval (the API answers 409). */
export function isGoLiveRefusal(err: unknown): err is ApiError {
  return err instanceof ApiError && err.status === 409;
}

/** True when orders reach a real-money account: brass and LIVE only then. */
export function isRealMoneyBroker(broker: BrokerInfo): boolean {
  return broker.kind !== 'simulated' && !broker.paper;
}

/** The broker as a trader reads it on a ticket. */
export function brokerName(broker: BrokerInfo): string {
  if (broker.kind === 'simulated') return 'Simulated, paper money';
  const name = broker.kind.charAt(0).toUpperCase() + broker.kind.slice(1);
  return `${name} ${broker.paper ? 'paper account' : 'live account'}`;
}

/**
 * The go-live ticket (UX-03): the strategy, the portfolios that follow it
 * and the broker. `portfolios` null means the list could not be read, an
 * empty list means only the default portfolio follows it once live.
 */
export function goLiveTicket(
  name: string,
  portfolios: readonly string[] | null,
  broker: BrokerInfo | null,
): StatusChangeTicket {
  return {
    kind: 'Approval ticket',
    live: broker ? isRealMoneyBroker(broker) : null,
    lines: [
      { label: 'Strategy', value: name },
      {
        label: 'Portfolios',
        value:
          portfolios === null
            ? 'Could not be read'
            : portfolios.length
              ? portfolios.join(', ')
              : 'The default portfolio',
      },
      { label: 'Broker', value: broker ? brokerName(broker) : 'Could not be read' },
    ],
  };
}

/**
 * One sentence on where the money is, built from the same broker as the
 * ticket's stamp (B2): PAPER never says real orders, LIVE always does.
 */
export function moneyLine(broker: BrokerInfo | null): string {
  if (!broker) return 'The broker could not be read. Check it on the Strategy review page first.';
  return isRealMoneyBroker(broker)
    ? 'Real money: portfolios that follow it send real orders to your broker from the next trading run.'
    : 'Portfolios that follow it place paper orders from the next trading run. No real money moves.';
}

export interface PromotionSteps<T> {
  /** Strategy id. */
  id: string;
  /** The name shown to the trader (defaults to the id). */
  name?: string;
  dialog: StatusChangeDialog;
  toasts: ToastService;
  /** The go-live report shown before asking. */
  golive: () => Promise<GoLiveReport>;
  /** The go-live call, made silently: this flow reports its own errors. */
  promote: (body: StatusChangeRequest) => Promise<T>;
  /**
   * The broker, read once for the ticket (UX-03). Without it the dialog
   * shows no ticket (older callers).
   */
  broker?: () => Promise<BrokerInfo>;
  /** Names of the portfolios that follow the strategy, for the ticket. */
  followers?: () => Promise<string[]>;
  /** Wording for the first dialog. */
  title: string;
  message: string;
  confirmLabel: string;
  /**
   * Skip the normal dialog and ask for the override straight away (the
   * admin's "Override..." on a strategy that is not ready, UX-23).
   */
  overrideFirst?: boolean;
  /** Called with `true` while a request runs. */
  busy?: (on: boolean) => void;
}

/**
 * Approve with the go-live check in front:
 * 1. load the go-live report, the broker and the followers, then show the
 *    ticket and the report with a reason field and a hold-to-confirm button
 *    (typing the name instead when real money moves);
 * 2. approve; on a 409 (gate refused) show the failing checks and offer an
 *    override that needs a reason of at least 20 characters and the typed
 *    word "override";
 * 3. approve again with `override: true`.
 * Resolves to the API's answer, or `null` when cancelled or failed (other
 * errors are toasted here).
 */
export async function promoteThroughGate<T>(steps: PromotionSteps<T>): Promise<T | null> {
  const { dialog, toasts } = steps;
  const name = steps.name ?? steps.id;
  let report: GoLiveReport | null = null;
  let note: string | null = null;
  let ticket: StatusChangeTicket | null = null;
  let broker: BrokerInfo | null = null;
  steps.busy?.(true);
  try {
    const [gate, brokerRead, followersRead] = await Promise.allSettled([
      steps.golive(),
      steps.broker?.() ?? Promise.resolve(null),
      steps.followers?.() ?? Promise.resolve(null),
    ]);
    if (gate.status === 'fulfilled') report = gate.value;
    else {
      note =
        `Could not run the go-live check first (${errorMessage(gate.reason)}). ` +
        'The server still applies it when you approve it.';
    }
    if (steps.broker) {
      broker = brokerRead.status === 'fulfilled' ? brokerRead.value : null;
      const followers = followersRead.status === 'fulfilled' ? followersRead.value : null;
      ticket = goLiveTicket(name, steps.followers ? followers : [], broker);
    }
  } finally {
    steps.busy?.(false);
  }
  const realMoney = ticket?.live === true;
  const message = ticket ? `${steps.message} ${moneyLine(broker)}` : steps.message;

  let refusal: string | null = null;
  if (!steps.overrideFirst) {
    const body = await dialog.open({
      title: steps.title,
      message,
      confirmLabel: steps.confirmLabel,
      minReason: 1,
      reasonHint: 'Why now? Kept in the status history.',
      hold: true,
      typedConfirmation: realMoney ? name : undefined,
      golive: report,
      goliveNote: note,
      ticket,
    });
    if (!body) return null;

    steps.busy?.(true);
    try {
      return await steps.promote(body);
    } catch (err) {
      if (!isGoLiveRefusal(err)) {
        toasts.error(errorMessage(err), err instanceof ApiError ? err.title : undefined);
        return null;
      }
      refusal = err.message;
    } finally {
      steps.busy?.(false);
    }
  }

  const override = await dialog.open({
    title: steps.overrideFirst
      ? `Approve ${name} without passing the check?`
      : `The go-live check refused ${name}`,
    message:
      `${refusal ? `${refusal} ` : ''}Approving anyway lets people follow it without the ` +
      'evidence the check asks for. ' +
      (ticket ? `${moneyLine(broker)} ` : '') +
      'The override and your reason are recorded.',
    confirmLabel: 'Override and approve',
    // Red only when real money moves (B2): a paper override is a quiet choice.
    tone: realMoney ? 'danger' : 'default',
    minReason: OVERRIDE_MIN_REASON,
    typedConfirmation: 'override',
    override: true,
    golive: report,
    goliveNote: note,
    ticket,
  });
  if (!override) return null;

  steps.busy?.(true);
  try {
    return await steps.promote(override);
  } catch (err) {
    toasts.error(errorMessage(err), err instanceof ApiError ? err.title : undefined);
    return null;
  } finally {
    steps.busy?.(false);
  }
}

/** What happens to positions when a strategy steps back: one plain line (UX-24). */
export const HELD_POSITIONS_LINE = 'Positions it already holds are not closed.';

/**
 * The dialog for Back on trial (`pause`) and Retire (`stop`), shared by the
 * strategy page and Studio so both read the same (UX-24). Neither is red:
 * red belongs to the kill switch alone (M9).
 */
export function demoteOptions(action: 'pause' | 'stop', name: string): StatusChangeOptions {
  if (action === 'stop') {
    return {
      title: `Retire ${name}?`,
      message:
        `It no longer decides from the next trading run: no orders for its followers and no test ` +
        `book. ${HELD_POSITIONS_LINE} Its reports and history stay.`,
      confirmLabel: LIFECYCLE.stop.label,
      minReason: 1,
    };
  }
  return {
    title: `Put ${name} back on trial?`,
    message:
      'People can no longer follow it for trades. Its test book keeps deciding on every run. ' +
      HELD_POSITIONS_LINE,
    confirmLabel: LIFECYCLE.pause.label,
    minReason: 1,
  };
}
