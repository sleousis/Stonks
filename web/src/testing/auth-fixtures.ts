import type { MeView } from '../app/api/models';

export const TRADER: MeView = {
  user_id: 'usr_1',
  email: 'ann@example.com',
  display_name: 'Ann',
  role: 'trader',
  via: 'session',
  scopes: ['read', 'trade', 'lab'],
  mfa_enrolled: true,
  mfa_fresh: false,
};

export const ADMIN: MeView = {
  ...TRADER,
  user_id: 'usr_admin',
  email: 'boss@example.com',
  display_name: 'Boss',
  role: 'admin',
  scopes: ['read', 'trade', 'lab', 'admin'],
};

export const UNAUTHORIZED = { status: 401, statusText: 'Unauthorized' };
export const FORBIDDEN = { status: 403, statusText: 'Forbidden' };

export function problem(status: number, detail: string) {
  return { title: 'x', status, detail };
}
