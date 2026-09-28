import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { allItems, unwrap } from './api-call';
import {
  approveOrderDraft,
  createOrderDraft,
  listOrderDrafts,
  rejectOrderDraft,
} from './generated/sdk.gen';
import type { OrderDraftCreate } from './generated/types.gen';

/**
 * Order drafts ("Suggested orders" on the Approvals page): orders the
 * assistant (or MCP) proposed. The server prices
 * them. Approving one places it as a manual order and needs a fresh second
 * factor (403 `step_up_required` until the code is confirmed).
 */
@Injectable({ providedIn: 'root' })
export class OrderDraftsService {
  list(status?: 'pending' | 'placed' | 'rejected' | 'expired' | 'cancelled') {
    return allItems((query) => unwrap(listOrderDrafts({ query: { ...query, status } })));
  }

  /** How many wait for a decision, read quietly for the Approvals badge. */
  async pendingCount(): Promise<number> {
    const first = await unwrap(
      listOrderDrafts({ query: { status: 'pending', limit: 1 }, headers: SILENT_HEADERS }),
    );
    return first.total;
  }

  create(body: OrderDraftCreate) {
    return unwrap(createOrderDraft({ body }));
  }

  /** Silent: the drafts page shows a refusal on the draft's own ticket. */
  approve(id: string) {
    return unwrap(approveOrderDraft({ path: { draft_id: id }, headers: SILENT_HEADERS }));
  }

  reject(id: string, note?: string) {
    return unwrap(rejectOrderDraft({ path: { draft_id: id }, body: { note } }));
  }
}
