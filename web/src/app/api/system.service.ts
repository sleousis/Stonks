import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  changeSystemSetting,
  getAlpacaStatus,
  getBrokerInfo,
  getRiskPolicy,
  getStarterSet,
  getSystemSetting,
  installStarterSet,
  listAssetClasses,
  listIntervals,
  listStrategyClasses,
  listSystemSettings,
  resetSystemSetting,
} from './generated/sdk.gen';
import type { SystemSettingChange, SystemSettingReset } from './generated/types.gen';

/**
 * Reference data and configuration: catalog, broker, risk policy, the
 * system settings an admin edits (stored as overrides on top of the TOML
 * config, audited) and the starter set.
 */
@Injectable({ providedIn: 'root' })
export class SystemService {
  strategyClasses() {
    return unwrap(listStrategyClasses());
  }

  assetClasses() {
    return unwrap(listAssetClasses());
  }

  intervals() {
    return unwrap(listIntervals());
  }

  broker() {
    return unwrap(getBrokerInfo());
  }

  alpacaStatus() {
    return unwrap(getAlpacaStatus());
  }

  riskPolicy() {
    return unwrap(getRiskPolicy());
  }

  /** Every editable system setting with its value, TOML value and history (admins). */
  settings() {
    return unwrap(listSystemSettings());
  }

  setting(key: string) {
    return unwrap(getSystemSetting({ path: { key } }));
  }

  /** Change one setting; needs a fresh second factor. 422 on a bad value. */
  changeSetting(key: string, body: SystemSettingChange) {
    return unwrap(changeSystemSetting({ path: { key }, body }));
  }

  /** Drop the override, so the TOML value applies again. */
  resetSetting(key: string, body: SystemSettingReset) {
    return unwrap(resetSystemSetting({ path: { key }, body }));
  }

  /** The starter strategies and whether each is registered. */
  starterSet() {
    return unwrap(getStarterSet());
  }

  /** Put the starter strategies On trial (admins, idempotent). */
  installStarterSet() {
    return unwrap(installStarterSet());
  }
}
