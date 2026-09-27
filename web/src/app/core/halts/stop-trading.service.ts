import { Injectable, signal } from '@angular/core';

/**
 * Opens the Stop trading sheet from anywhere: the session strip's button
 * and the command palette's "Stop trading" both set `open`. The sheet
 * itself lives in the session strip.
 */
@Injectable({ providedIn: 'root' })
export class StopTradingService {
  readonly open = signal(false);
}
