import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { createTelegramLinkCode, deleteTelegramLink, getTelegramLink } from './generated/sdk.gen';

/**
 * Your Telegram link: its status, a one-time code to send the bot as
 * `/link CODE` (shown once), and unlinking.
 */
@Injectable({ providedIn: 'root' })
export class TelegramService {
  status() {
    return unwrap(getTelegramLink());
  }

  createCode() {
    return unwrap(createTelegramLinkCode());
  }

  unlink() {
    return unwrap(deleteTelegramLink());
  }
}
