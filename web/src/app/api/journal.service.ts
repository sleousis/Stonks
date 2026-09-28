import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';

import { unwrap } from './api-call';
import {
  createPlaybook,
  getJournalBreakdown,
  getJournalTrade,
  getPnlCalendar,
  listJournalLabels,
  listJournalTrades,
  listPlaybooks,
  reviewJournalTrade,
  updatePlaybook,
} from './generated/sdk.gen';
import type {
  AnnotationRequest,
  GetJournalBreakdownData,
  GetPnlCalendarData,
  ListJournalTradesData,
  PlaybookCreate,
  PlaybookUpdate,
} from './models';

/** The round-trip journal (roadmap 23.3), for the picked portfolio. */
@Injectable({ providedIn: 'root' })
export class JournalService {
  private readonly ctx = inject(PortfolioContextService);

  trades(query?: ListJournalTradesData['query']) {
    return unwrap(listJournalTrades({ query: { ...this.ctx.query(), ...query } }));
  }

  trade(tradeId: number) {
    return unwrap(getJournalTrade({ path: { trade_id: tradeId }, query: this.ctx.query() }));
  }

  review(tradeId: number, body: AnnotationRequest) {
    return unwrap(
      reviewJournalTrade({ path: { trade_id: tradeId }, query: this.ctx.query(), body }),
    );
  }

  calendar(query?: GetPnlCalendarData['query']) {
    return unwrap(getPnlCalendar({ query: { ...this.ctx.query(), ...query } }));
  }

  breakdown(query?: GetJournalBreakdownData['query']) {
    return unwrap(getJournalBreakdown({ query: { ...this.ctx.query(), ...query } }));
  }

  labels() {
    return unwrap(listJournalLabels({ query: this.ctx.query() }));
  }

  playbooks(includeArchived = false) {
    return unwrap(listPlaybooks({ query: { include_archived: includeArchived, limit: 500 } })).then(
      (page) => page.items,
    );
  }

  createPlaybook(body: PlaybookCreate) {
    return unwrap(createPlaybook({ body }));
  }

  updatePlaybook(id: string, body: PlaybookUpdate) {
    return unwrap(updatePlaybook({ path: { playbook_id: id }, body }));
  }
}
