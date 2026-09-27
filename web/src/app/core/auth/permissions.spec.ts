import type { MeView } from '../../api/models';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { POLICY, allowed, denial } from './permissions';
import { ROUTE_PERMISSIONS } from './route-permissions.gen';

const VIEWER: MeView = { ...TRADER, role: 'viewer', scopes: ['read'] };

describe('permissions', () => {
  it('has a rule for every permission a route asks for', () => {
    for (const permission of Object.values(ROUTE_PERMISSIONS)) {
      expect(POLICY[permission], permission).toBeDefined();
    }
  });

  it('reads the route permissions from the contract', () => {
    expect(ROUTE_PERMISSIONS['POST /api/strategies/{strategy_id}/promote']).toBe(
      'strategy.promote',
    );
    expect(ROUTE_PERMISSIONS['POST /api/ticks']).toBe('operations.run');
  });

  it('lets admins promote and run operations, not traders or viewers', () => {
    expect(allowed(ADMIN, 'strategy.promote')).toBe(true);
    expect(allowed(TRADER, 'strategy.promote')).toBe(false);
    expect(allowed(VIEWER, 'operations.run')).toBe(false);
    expect(denial(TRADER, 'operations.run')).toBe('Admins only.');
  });

  it('lets traders run the lab and manage connections, not viewers', () => {
    expect(allowed(TRADER, 'lab.run')).toBe(true);
    expect(allowed(TRADER, 'connection.manage')).toBe(true);
    expect(denial(VIEWER, 'lab.run')).toBe('Traders and admins only.');
  });

  it('checks the credential scopes, not only the role', () => {
    const readOnlyToken: MeView = { ...ADMIN, via: 'token', scopes: ['read'] };
    expect(allowed(readOnlyToken, 'strategy.promote')).toBe(false);
    expect(denial(readOnlyToken, 'strategy.promote')).toContain('API token');
  });

  it('keeps token management to browser sessions', () => {
    expect(allowed(TRADER, 'tokens.manage')).toBe(true);
    expect(allowed({ ...TRADER, via: 'token' }, 'tokens.manage')).toBe(false);
  });

  it('denies everything when nobody is signed in', () => {
    expect(allowed(null, 'data.read')).toBe(false);
    expect(denial(null, 'lab.run')).toBe('Sign in to do this.');
  });
});
