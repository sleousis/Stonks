import { TestBed } from '@angular/core/testing';
import {
  type ActivatedRouteSnapshot,
  type CanActivateFn,
  type RouterStateSnapshot,
  Router,
  UrlTree,
  provideRouter,
} from '@angular/router';

import type { MeView } from '../../api/models';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { adminGuard, authGuard, guestGuard, protectRoutes, safeNext } from './auth.guards';
import { type SessionStatus, SessionService } from './session.service';

/** A SessionService stand-in whose load() answers with a fixed status. */
function fakeSession(status: SessionStatus, me: MeView | null = null) {
  return {
    load: vi.fn(async () => status),
    status: () => status,
    me: () => me,
    isAdmin: () => me?.role === 'admin',
  };
}

describe('auth guards', () => {
  function run(guard: CanActivateFn, session: ReturnType<typeof fakeSession>, url = '/lab') {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: SessionService, useValue: session }],
    });
    const route = { queryParamMap: new Map() } as unknown as ActivatedRouteSnapshot;
    return TestBed.runInInjectionContext(() => guard(route, { url } as RouterStateSnapshot));
  }

  function serialize(result: unknown): string {
    expect(result).toBeInstanceOf(UrlTree);
    return TestBed.inject(Router).serializeUrl(result as UrlTree);
  }

  describe('authGuard', () => {
    it('lets signed-in people through', async () => {
      expect(await run(authGuard, fakeSession('signed-in', TRADER))).toBe(true);
    });

    it('lets people through when reads are open (dev) or the API is down', async () => {
      expect(await run(authGuard, fakeSession('open'))).toBe(true);
      TestBed.resetTestingModule();
      expect(await run(authGuard, fakeSession('unreachable'))).toBe(true);
    });

    it('sends signed-out people to sign-in and back afterwards', async () => {
      expect(serialize(await run(authGuard, fakeSession('signed-out')))).toBe('/login?next=%2Flab');
    });

    it('sends a pending second factor to the code screen', async () => {
      expect(serialize(await run(authGuard, fakeSession('mfa-pending')))).toBe(
        '/login?step=code&next=%2Flab',
      );
    });
  });

  describe('adminGuard', () => {
    it('lets admins through', async () => {
      expect(await run(adminGuard, fakeSession('signed-in', ADMIN))).toBe(true);
    });

    it('sends everyone else home', async () => {
      expect(serialize(await run(adminGuard, fakeSession('signed-in', TRADER)))).toBe('/');
    });
  });

  describe('guestGuard', () => {
    it('shows sign-in to signed-out people', async () => {
      expect(await run(guestGuard, fakeSession('signed-out'))).toBe(true);
    });

    it('sends signed-in people home', async () => {
      expect(serialize(await run(guestGuard, fakeSession('signed-in', TRADER)))).toBe('/');
    });
  });
});

describe('protectRoutes', () => {
  it('guards every top-level route except public ones', () => {
    const routes = protectRoutes([
      { path: 'lab' },
      { path: 'login', data: { public: true } },
      { path: 'x', canActivate: [() => true] },
      { path: 'old', redirectTo: 'lab' },
    ]);
    expect(routes[3].canActivate).toBeUndefined();
    expect(routes[0].canActivate).toEqual([authGuard]);
    expect(routes[1].canActivate).toBeUndefined();
    expect(routes[2].canActivate).toHaveLength(2);
    expect(routes[2].canActivate?.[0]).toBe(authGuard);
  });
});

describe('safeNext', () => {
  it('keeps same-app paths only', () => {
    expect(safeNext('/lab?x=1')).toBe('/lab?x=1');
    expect(safeNext(null)).toBe('/');
    expect(safeNext('https://evil.example')).toBe('/');
    expect(safeNext('//evil.example')).toBe('/');
    expect(safeNext('/\\evil.example')).toBe('/');
    expect(safeNext('/login?next=/x')).toBe('/');
  });
});
