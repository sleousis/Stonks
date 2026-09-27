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
 * and the Schedule page show a real run as an order ticket (`tickTicket`).
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
  if (!broker) throw new Error('Load the broker before a real trading run.');
  const label = brokerLabel(broker);
  const where = isLiveBroker(broker)
    ? `Orders go to the ${label} broker and trade real money.`
    : `Orders go to the ${label} broker and are recorded in the ledger.`;
  return {
    title: `Start a trading run on the ${label} broker?`,
    message: `${where} Paper trading strategies are checked afterwards.`,
    confirmLabel: 'Start trading run',
    tone: 'danger',
    typedConfirmation: label,
  };
}

/**
 * A real trading run as an order ticket for `ConfirmService.confirm`: the
 * broker's PAPER or LIVE stamp, the broker, date and tickers as ticket
 * lines, and the broker label typed to confirm.
 */
export function tickTicket(
  broker: BrokerInfo,
  form: { asOf: string; tickers: string },
  title = 'Trading run ticket',
): ConfirmOptions {
  const options = tickConfirmOptions(false, broker);
  const request = tickRequest({ dryRun: false, ...form });
  return {
    ...options,
    title,
    cancelLabel: 'Keep editing',
    ticket: {
      live: isLiveBroker(broker),
      lines: [
        { label: 'Broker', value: brokerLabel(broker) },
        { label: 'As of', value: request.as_of ?? 'Today' },
        { label: 'Tickers', value: request.tickers?.join(', ') ?? 'All in the universe' },
        { label: 'Strategies', value: 'Every live strategy' },
      ],
    },
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
