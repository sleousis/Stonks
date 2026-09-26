// Contract types for components and pages. Import models from here, never
// from ./generated directly, so a generator swap only touches src/app/api/.
export type * from './generated/types.gen';

/** Paged list shape shared by every `Page_*_` response. */
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type StrategyStatus = 'active' | 'shadow' | 'retired';
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = ['succeeded', 'failed', 'cancelled'];

/** An account a broker connection found (the connections flavour of BrokerAccountView). */
export type { StonksAppConnectionsBrokerAccountView as ConnectedAccountView } from './generated/types.gen';
