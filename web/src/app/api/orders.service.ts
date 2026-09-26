import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { listFills, listOrders } from './generated/sdk.gen';
import type { ListFillsData, ListOrdersData } from './models';

/** Orders placed by ticks and their fills. */
@Injectable({ providedIn: 'root' })
export class OrdersService {
  list(query?: ListOrdersData['query']) {
    return unwrap(listOrders({ query }));
  }

  fills(query?: ListFillsData['query']) {
    return unwrap(listFills({ query }));
  }
}
