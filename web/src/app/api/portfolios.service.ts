import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { toApiError } from '../core/http/api-error';
import { SILENT_HEADERS } from '../core/http/interceptors';

/*
 * The caller's portfolios. `GET /api/portfolios` is not in openapi.json yet
 * (the backend is adding it), so this calls the planned route with
 * HttpClient (interceptors still apply) and the type below stands in for
 * the generated one. When the route lands, swap the body for the SDK call
 * and re-export the type from ./models.
 *
 *   GET /api/portfolios -> PortfolioRef[]   (the caller's own; admins too)
 */
export interface PortfolioRef {
  id: string;
  name: string;
  /** Paper portfolios trade with simulated money; live ones with real money. */
  mode?: 'paper' | 'live' | null;
  currency?: string | null;
  /** The one reads use when no `portfolio_id` is sent. */
  is_default?: boolean | null;
}

@Injectable({ providedIn: 'root' })
export class PortfoliosService {
  private readonly http = inject(HttpClient);

  /** Silent: a server without the route answers 404 and the picker just stays hidden. */
  async list(): Promise<PortfolioRef[]> {
    try {
      return await firstValueFrom(
        this.http.get<PortfolioRef[]>('/api/portfolios', { headers: SILENT_HEADERS }),
      );
    } catch (err) {
      throw toApiError(err);
    }
  }
}
