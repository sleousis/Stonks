import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { allItems, unwrap } from './api-call';
import {
  createWatchlist,
  deleteWatchlist,
  getWatchlist,
  listWatchlists,
  updateWatchlist,
} from './generated/sdk.gen';
import type { WatchlistCreate, WatchlistUpdate } from './models';

/** Your watchlists: named ticker lists, never shared. */
@Injectable({ providedIn: 'root' })
export class WatchlistsService {
  /** Every list of yours. `silent` for pickers that show their own state. */
  list(silent = false) {
    const headers = silent ? SILENT_HEADERS : undefined;
    return allItems((query) => unwrap(listWatchlists({ query, headers })));
  }

  get(id: string) {
    return unwrap(getWatchlist({ path: { watchlist_id: id } }));
  }

  create(body: WatchlistCreate) {
    return unwrap(createWatchlist({ body }));
  }

  update(id: string, body: WatchlistUpdate) {
    return unwrap(updateWatchlist({ path: { watchlist_id: id }, body }));
  }

  delete(id: string) {
    return unwrap(deleteWatchlist({ path: { watchlist_id: id } }));
  }
}
