import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { allItems, unwrap } from './api-call';
import {
  createScreen,
  deleteScreen,
  getScreen,
  listScreenMetrics,
  listScreens,
  runScreen,
  saveScreenAsUniverse,
  updateScreen,
} from './generated/sdk.gen';
import type {
  SavedScreenCreate,
  SavedScreenUpdate,
  ScreenRunRequest,
  ScreenUniverseRequest,
} from './models';

/** Screens on price and fundamental metrics, saved screens, and screens as universes. */
@Injectable({ providedIn: 'root' })
export class ScreenerService {
  metrics() {
    return unwrap(listScreenMetrics());
  }

  run(body: ScreenRunRequest) {
    return unwrap(runScreen({ body }));
  }

  /** Every saved screen of yours. */
  list() {
    return allItems((query) => unwrap(listScreens({ query })));
  }

  get(id: string) {
    return unwrap(getScreen({ path: { screen_id: id } }));
  }

  create(body: SavedScreenCreate) {
    return unwrap(createScreen({ body }));
  }

  update(id: string, body: SavedScreenUpdate) {
    return unwrap(updateScreen({ path: { screen_id: id }, body }));
  }

  delete(id: string) {
    return unwrap(deleteScreen({ path: { screen_id: id } }));
  }

  /**
   * Store a screen as a universe for the lab (queues its refresh). Silent:
   * the dialog shows a failure where the trader is looking.
   */
  saveAsUniverse(body: ScreenUniverseRequest) {
    return unwrap(saveScreenAsUniverse({ body, headers: SILENT_HEADERS }));
  }
}
