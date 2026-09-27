import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';

import { unwrap } from './api-call';
import { listFills, listOrders } from './generated/sdk.gen';
import type { ListFillsData, ListOrdersData } from './models';

/** Orders placed by ticks and their fills, for the picked portfolio. */
@Injectable({ providedIn: 'root' })
export class OrdersService {
  private readonly ctx = inject(PortfolioContextService);

  list(query?: ListOrdersData['query']) {
    return unwrap(listOrders({ query: { ...this.ctx.query(), ...query } }));
  }

  fills(query?: ListFillsData['query']) {
    return unwrap(listFills({ query: { ...this.ctx.query(), ...query } }));
  }
}
