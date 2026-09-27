import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';

import { unwrap } from './api-call';
import {
  cancelOrder,
  changeManualOrder,
  placeManualOrder,
  previewManualOrder,
} from './generated/sdk.gen';
import type {
  ManualOrderChange,
  ManualOrderRequest,
  OrderCancelRequest,
} from './generated/types.gen';

/**
 * Manual orders on the picked portfolio: preview (every check, nothing
 * placed), place, change (cancel and replace) and cancel. A real-money book
 * answers 403 `step_up_required` until the second factor is fresh. A refused
 * order is 409 `order_refused` with the risk rules' adjustments.
 */
@Injectable({ providedIn: 'root' })
export class ManualOrdersService {
  private readonly ctx = inject(PortfolioContextService);

  preview(body: ManualOrderRequest) {
    return unwrap(previewManualOrder({ body: this.withPortfolio(body) }));
  }

  place(body: ManualOrderRequest) {
    return unwrap(placeManualOrder({ body: this.withPortfolio(body) }));
  }

  change(clientId: string, body: ManualOrderChange) {
    return unwrap(
      changeManualOrder({ path: { client_id: clientId }, body: this.withPortfolio(body) }),
    );
  }

  cancel(clientId: string, body: OrderCancelRequest) {
    return unwrap(cancelOrder({ path: { client_id: clientId }, body: this.withPortfolio(body) }));
  }

  private withPortfolio<T extends { portfolio_id?: string | null }>(body: T): T {
    return { portfolio_id: this.ctx.query().portfolio_id ?? null, ...body };
  }
}
