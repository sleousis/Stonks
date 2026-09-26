import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { type Observable, firstValueFrom } from 'rxjs';

import { ApiError, toApiError } from '../core/http/api-error';
import type { StrategyStatus } from './models';

/*
 * The trader's strategy subscriptions (design: accounts-and-modes.md,
 * section 4). The backend has the repository (accounts/subscriptions.py)
 * but no REST routes yet, so this service calls the planned routes with
 * HttpClient directly (interceptors still apply) and these types stand in
 * for the generated ones. When the routes land in openapi.json, swap the
 * bodies for generated SDK calls and re-export the types from ./models.
 *
 *   GET   /api/subscriptions        -> SubscriptionView[]   (the caller's own)
 *   PATCH /api/subscriptions/{id}   <- { enabled?, mode?, reason? } -> SubscriptionView
 *         mode 'auto' needs a fresh second factor (403 step_up_required)
 *         and passes the auto gate (409 with the blockers otherwise).
 */

export type SubscriptionMode = 'notify' | 'paper' | 'auto';

export interface SubscriptionView {
  id: string;
  strategy_id: string;
  strategy_status: StrategyStatus;
  portfolio_id: string | null;
  mode: SubscriptionMode;
  enabled: boolean;
  paper_days_completed: number;
  /** MIN_PAPER_DAYS_FOR_AUTO on the server (20). */
  paper_days_required: number;
  /** Why auto is refused right now, in words; empty when it is allowed. */
  auto_blockers: string[];
  /** Set while auto is paused (broker error, kill switch, ...). */
  paused_reason: string | null;
}

export interface SubscriptionUpdate {
  enabled?: boolean;
  mode?: SubscriptionMode;
  reason?: string;
}

/** True when the API has no subscription routes yet (older server). */
export function isMissingRoute(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 404 || err.status === 405);
}

@Injectable({ providedIn: 'root' })
export class SubscriptionsService {
  private readonly http = inject(HttpClient);

  list(): Promise<SubscriptionView[]> {
    return this.call(this.http.get<SubscriptionView[]>('/api/subscriptions'));
  }

  update(id: string, body: SubscriptionUpdate): Promise<SubscriptionView> {
    return this.call(
      this.http.patch<SubscriptionView>(`/api/subscriptions/${encodeURIComponent(id)}`, body),
    );
  }

  private async call<T>(request: Observable<T>): Promise<T> {
    try {
      return await firstValueFrom(request);
    } catch (err) {
      throw toApiError(err);
    }
  }
}
