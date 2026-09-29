import { signal } from '@angular/core';

import type { ConnectedAccountView, ConnectionView, ProviderView } from '../../api/models';
import type { Permission } from '../../core/auth/permissions';

export const ALPACA: ProviderView = {
  name: 'alpaca',
  display_name: 'Alpaca',
  auth_flow: 'api_key',
  capabilities: ['read_activity', 'read_balances', 'read_positions', 'trade'],
  credential_fields: ['api_key', 'secret_key'],
  can_trade: true,
  enabled: true,
  has_paper: true,
  needs_gateway: false,
};

export const SNAPTRADE: ProviderView = {
  name: 'snaptrade',
  display_name: 'SnapTrade',
  auth_flow: 'portal',
  capabilities: ['read_activity', 'read_balances', 'read_positions'],
  credential_fields: [],
  can_trade: false,
  enabled: true,
  has_paper: false,
  needs_gateway: false,
};

export const ETORO: ProviderView = {
  name: 'etoro',
  display_name: 'eToro',
  auth_flow: 'api_key',
  capabilities: ['read_activity', 'read_balances', 'read_positions', 'trade'],
  credential_fields: ['api_key', 'user_key'],
  can_trade: true,
  enabled: true,
  has_paper: true,
  needs_gateway: false,
};

export function connection(over: Partial<ConnectionView> = {}): ConnectionView {
  return {
    id: 'con_1',
    provider: 'alpaca',
    label: null,
    status: 'active',
    last_sync_at: null,
    last_sync_status: null,
    last_error: null,
    consecutive_failures: 0,
    next_sync_at: null,
    created_at: '2026-09-26T09:00:00Z',
    updated_at: '2026-09-26T09:00:00Z',
    accounts_count: 0,
    ...over,
  };
}

export function account(over: Partial<ConnectedAccountView> = {}): ConnectedAccountView {
  return {
    connection_id: 'con_1',
    external_account_id: 'acc_1',
    name: 'Individual',
    institution: 'Alpaca',
    number_mask: '1234',
    currency: 'USD',
    portfolio_id: null,
    ...over,
  };
}

/** A SessionService stand-in that grants everything, or nothing (a viewer). */
export function sessionStub(allowed: boolean) {
  return {
    can: (permission: Permission) => allowed || !permission,
    whyNot: (permission: Permission) => (allowed || !permission ? null : 'Traders only.'),
    me: signal(null),
    // Read by the session interceptor on every unsafe call.
    csrfToken: () => null,
    status: signal('signed-in'),
  };
}
