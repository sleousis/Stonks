import { Injectable } from '@angular/core';

import { allItems, unwrap } from './api-call';
import {
  approveOrderDraft,
  createOrderDraft,
  listOrderDrafts,
  rejectOrderDraft,
} from './generated/sdk.gen';
import type { OrderDraftCreate } from './generated/types.gen';

/**
 * Order drafts: orders the assistant (or MCP) proposed. The server prices
 * them. Approving one places it as a manual order and needs a fresh second
 * factor (403 `step_up_required` until the code is confirmed).
 */
@Injectable({ providedIn: 'root' })
export class OrderDraftsService {
  list(status?: 'pending' | 'placed' | 'rejected' | 'expired' | 'cancelled') {
    return allItems((query) => unwrap(listOrderDrafts({ query: { ...query, status } })));
  }

  create(body: OrderDraftCreate) {
    return unwrap(createOrderDraft({ body }));
  }

  approve(id: string) {
    return unwrap(approveOrderDraft({ path: { draft_id: id } }));
  }

  reject(id: string, note?: string) {
    return unwrap(rejectOrderDraft({ path: { draft_id: id }, body: { note } }));
  }
}
