import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { allItems, unwrap } from './api-call';
import { approveTickets, getTicketSummary, listTickets, rejectTicket } from './generated/sdk.gen';
import type { TicketView } from './generated/types.gen';

export type { TicketSummary, TicketView } from './generated/types.gen';
export type TicketStatus = TicketView['status'];

/**
 * Order tickets (roadmap 19.8): the orders a live book decided after the
 * close. Approve mode tickets, and the closes of a runaway run, wait for a
 * person. Approving needs a fresh second factor (403 `step_up_required`
 * until the code is confirmed; one code covers a whole batch).
 */
@Injectable({ providedIn: 'root' })
export class TicketsService {
  list(status?: TicketStatus) {
    return allItems((query) => unwrap(listTickets({ query: { ...query, status } })));
  }

  /** `silent`: no error toast (the nav badge polls it quietly). */
  summary(silent = false) {
    return unwrap(getTicketSummary({ headers: silent ? SILENT_HEADERS : undefined }));
  }

  approve(ids: readonly string[]) {
    return unwrap(approveTickets({ body: { ticket_ids: [...ids] } }));
  }

  reject(id: string, reason: string) {
    return unwrap(rejectTicket({ path: { ticket_id: id }, body: { reason } }));
  }
}
