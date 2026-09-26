import { Injectable } from '@angular/core';

import { allItems, unwrap } from './api-call';
import {
  completeConnectionPortal,
  connectWithKeys,
  deleteConnection,
  getConnection,
  linkConnectionAccount,
  listConnectionAccounts,
  listConnections,
  listProviders,
  startConnectionPortal,
  syncConnection,
} from './generated/sdk.gen';
import type {
  CompleteConnectionPortalData,
  ConnectWithKeysRequestWritable,
  LinkAccountRequest,
  StartPortalRequest,
} from './models';

/**
 * Broker connections of the signed-in user: providers, connect (API keys or
 * a hosted portal), link accounts to portfolios, sync and remove. Credentials
 * go out in `connectWithKeys` only and never come back.
 */
@Injectable({ providedIn: 'root' })
export class ConnectionsService {
  providers() {
    return unwrap(listProviders());
  }

  list() {
    return allItems((query) => unwrap(listConnections({ query })));
  }

  get(connectionId: string) {
    return unwrap(getConnection({ path: { connection_id: connectionId } }));
  }

  accounts(connectionId: string) {
    return allItems((query) =>
      unwrap(listConnectionAccounts({ path: { connection_id: connectionId }, query })),
    );
  }

  connectWithKeys(body: ConnectWithKeysRequestWritable) {
    return unwrap(connectWithKeys({ body }));
  }

  startPortal(body: StartPortalRequest) {
    return unwrap(startConnectionPortal({ body }));
  }

  /** The portal's redirect carries `connection_id` and `state`; pass them on. */
  completePortal(query: CompleteConnectionPortalData['query']) {
    return unwrap(completeConnectionPortal({ query }));
  }

  link(connectionId: string, body: LinkAccountRequest) {
    return unwrap(linkConnectionAccount({ path: { connection_id: connectionId }, body }));
  }

  sync(connectionId: string) {
    return unwrap(syncConnection({ path: { connection_id: connectionId } }));
  }

  remove(connectionId: string) {
    return unwrap(deleteConnection({ path: { connection_id: connectionId } }));
  }
}
