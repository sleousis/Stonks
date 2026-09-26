import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  createUser,
  listUsers,
  resetUserMfa,
  resetUserPassword,
  updateUser,
} from './generated/sdk.gen';
import type { UserCreateRequest, UserUpdateRequest } from './models';

/**
 * People with an account (admins only). Identity, role and status, never
 * holdings. Changes need a fresh second factor; the session interceptor
 * asks for it when the API says so.
 */
@Injectable({ providedIn: 'root' })
export class UsersService {
  list() {
    return unwrap(listUsers());
  }

  create(body: UserCreateRequest) {
    return unwrap(createUser({ body }));
  }

  update(userId: string, body: UserUpdateRequest) {
    return unwrap(updateUser({ path: { user_id: userId }, body }));
  }

  resetMfa(userId: string) {
    return unwrap(resetUserMfa({ path: { user_id: userId } }));
  }

  resetPassword(userId: string, newPassword: string) {
    return unwrap(
      resetUserPassword({ path: { user_id: userId }, body: { new_password: newPassword } }),
    );
  }
}
