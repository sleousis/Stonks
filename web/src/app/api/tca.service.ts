import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';

import { unwrap } from './api-call';
import {
  addJournalNote,
  getOrderTca,
  getTcaSummary,
  listJournal,
  updateJournalNote,
} from './generated/sdk.gen';
import type { GetTcaSummaryData, ListJournalData } from './models';

/** Trade costs (implementation shortfall) and the trade journal, for the picked portfolio. */
@Injectable({ providedIn: 'root' })
export class TcaService {
  private readonly ctx = inject(PortfolioContextService);

  summary(query?: GetTcaSummaryData['query']) {
    return unwrap(getTcaSummary({ query: { ...this.ctx.query(), ...query } }));
  }

  journal(query?: ListJournalData['query']) {
    return unwrap(listJournal({ query: { ...this.ctx.query(), ...query } }));
  }

  order(clientId: string) {
    return unwrap(getOrderTca({ path: { client_id: clientId } }));
  }

  addNote(clientId: string, note: string) {
    return unwrap(addJournalNote({ path: { client_id: clientId }, body: { note } }));
  }

  editNote(noteId: number, note: string) {
    return unwrap(updateJournalNote({ path: { note_id: noteId }, body: { note } }));
  }
}
