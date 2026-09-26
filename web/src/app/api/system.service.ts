import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  getAlpacaStatus,
  getBrokerInfo,
  getRiskPolicy,
  listAssetClasses,
  listIntervals,
  listStrategyClasses,
} from './generated/sdk.gen';

/** Reference data and configuration: catalog, broker, risk policy. */
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
}
