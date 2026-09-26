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
 * What the confirm dialog asks before a tick. A dry run only needs a click;
 * a real tick names the broker and needs the broker label typed out, so the
 * trader has read where the orders go before they go there.
 */
export function tickConfirmOptions(dryRun: boolean, broker: BrokerInfo | null): ConfirmOptions {
  if (dryRun) {
    return {
      title: 'Run a dry-run tick?',
      message:
        'Strategies decide and orders are sized, but nothing is sent to the broker and no fills are recorded.',
      confirmLabel: 'Run dry run',
    };
  }
  if (!broker) throw new Error('A real tick needs the broker configuration first.');
  const label = brokerLabel(broker);
  const where = isLiveBroker(broker)
    ? `Orders go to the ${label} broker and trade real money.`
    : `Orders go to the ${label} broker and are recorded in the ledger.`;
  return {
    title: `Run a real tick on the ${label} broker?`,
    message: `${where} Shadow strategies are evaluated afterwards.`,
    confirmLabel: 'Run tick',
    tone: 'danger',
    typedConfirmation: label,
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
