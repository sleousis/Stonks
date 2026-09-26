import type { BrokerInfo, TickRequest } from '../../api/models';
import type { ConfirmOptions } from '../../core/confirm/confirm.service';

/** "simulated", "alpaca paper" or "alpaca live": what the trader types to confirm. */
export function brokerLabel(broker: BrokerInfo): string {
  if (broker.kind === 'simulated') return 'simulated';
  return `${broker.kind} ${broker.paper ? 'paper' : 'live'}`;
}

/** True when orders would reach a real-money account. */
export function isLiveBroker(broker: BrokerInfo): boolean {
  return broker.kind !== 'simulated' && !broker.paper;
}

/**
 * What the confirm dialog asks before a trading run. A dry run only needs a
 * click; a real run names the broker and needs the broker label typed out, so
 * the trader has read where the orders go before they go there. The runner
 * shows a real run as an order ticket (`tickTicket`); these options are the
 * plain-text fallback and the dry run's dialog.
 */
export function tickConfirmOptions(dryRun: boolean, broker: BrokerInfo | null): ConfirmOptions {
  if (dryRun) {
    return {
      title: 'Start a dry run?',
      message:
        'Strategies decide and orders are sized, but nothing is sent to the broker and no fills are recorded.',
      confirmLabel: 'Start dry run',
    };
  }
  if (!broker) throw new Error('A real tick needs the broker configuration first.');
  const label = brokerLabel(broker);
  const where = isLiveBroker(broker)
    ? `Orders go to the ${label} broker and trade real money.`
    : `Orders go to the ${label} broker and are recorded in the ledger.`;
  return {
    title: `Start a trading run on the ${label} broker?`,
    message: `${where} Shadow strategies are evaluated afterwards.`,
    confirmLabel: 'Start trading run',
    tone: 'danger',
    typedConfirmation: label,
  };
}

/** One line of the order ticket. */
export interface TicketLine {
  label: string;
  value: string;
  /** Tabular mono figures (dates, tickers). */
  mono?: boolean;
}

/** A real trading run shown as an order ticket before it goes. */
export interface TickTicket {
  title: string;
  /** Real money: the ticket carries a brass LIVE stamp, else a grey PAPER one. */
  live: boolean;
  lines: TicketLine[];
  message: string;
  confirmLabel: string;
  /** Typed to confirm: the broker label. */
  typedConfirmation: string;
}

/** The ticket for a real run, from the broker and the runner's form. */
export function tickTicket(
  broker: BrokerInfo,
  form: { asOf: string; tickers: string },
): TickTicket {
  const options = tickConfirmOptions(false, broker);
  const request = tickRequest({ dryRun: false, ...form });
  return {
    title: 'Trading run ticket',
    live: isLiveBroker(broker),
    lines: [
      { label: 'Broker', value: brokerLabel(broker) },
      { label: 'As of', value: request.as_of ?? 'Today', mono: !!request.as_of },
      {
        label: 'Tickers',
        value: request.tickers?.join(', ') ?? 'All in the universe',
        mono: !!request.tickers,
      },
      { label: 'Strategies', value: 'Every active strategy' },
    ],
    message: options.message,
    confirmLabel: options.confirmLabel,
    typedConfirmation: options.typedConfirmation ?? brokerLabel(broker),
  };
}

/** Form values to the API body: blank fields are left to the server's defaults. */
export function tickRequest(form: { dryRun: boolean; asOf: string; tickers: string }): TickRequest {
  const tickers = form.tickers
    .split(/[\s,]+/)
    .map((t) => t.trim().toUpperCase())
    .filter(Boolean);
  return {
    dry_run: form.dryRun,
    as_of: form.asOf || null,
    tickers: tickers.length ? tickers : null,
  };
}
