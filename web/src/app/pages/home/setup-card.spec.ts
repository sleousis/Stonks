import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { SetupCard } from './setup-card';

const VIEW = {
  steps: [
    { id: 'account', state: 'done', derived: true, optional: false },
    { id: 'portfolio', state: 'skipped', derived: false, optional: true },
    { id: 'data', state: 'todo', derived: false, optional: true },
    { id: 'follow', state: 'todo', derived: false, optional: false },
    { id: 'alerts', state: 'todo', derived: false, optional: false },
  ],
  complete: false,
  dismissed: false,
  show: true,
};

describe('SetupCard', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(http, '/api/auth/me')).flush(TRADER);
    await loading;
  });

  afterEach(() => http.verify());

  it('shows progress and the next step until the guide is hidden', async () => {
    const fixture = TestBed.createComponent(SetupCard);
    fixture.detectChanges();
    (await nextRequest(http, '/api/onboarding')).flush(VIEW);
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('1 of 3 done');
    expect(el.querySelector('h2')!.textContent).toBe('Get set up');
    // The no-broker path comes first (FR-01).
    expect(el.textContent).toContain('Next: follow a strategy');
    expect(el.querySelector('a')?.getAttribute('href')).toBe('/welcome');

    [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Hide'))!.click();
    const req = await nextRequest(http, '/api/onboarding', 'PUT');
    expect(req.request.body).toEqual({ dismissed: true });
    req.flush({ ...VIEW, dismissed: true, show: false });
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('section')).toBeNull();
  });

  it('leaves Today once only the optional steps are left (FR-01)', async () => {
    const fixture = TestBed.createComponent(SetupCard);
    fixture.detectChanges();
    const steps = VIEW.steps.map((s) =>
      s.id === 'follow' || s.id === 'alerts' ? { ...s, state: 'done' } : s,
    );
    (await nextRequest(http, '/api/onboarding')).flush({ ...VIEW, steps });
    await tick();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('section')).toBeNull();
  });

  it('holds its place while loading only if it showed last time', async () => {
    localStorage.removeItem(`stonks.setupShown.${TRADER.user_id}`);
    let fixture = TestBed.createComponent(SetupCard);
    fixture.detectChanges();
    // A new trader: the guide is likely, so its place is held.
    expect(fixture.nativeElement.querySelector('.reserve')).not.toBeNull();
    (await nextRequest(http, '/api/onboarding')).flush({ ...VIEW, show: false });
    await tick();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.reserve')).toBeNull();
    expect(localStorage.getItem(`stonks.setupShown.${TRADER.user_id}`)).toBe('0');

    // Next visit: the guide was closed, so nothing is held.
    fixture.destroy();
    fixture = TestBed.createComponent(SetupCard);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.reserve')).toBeNull();
    (await nextRequest(http, '/api/onboarding')).flush({ ...VIEW, show: false });
    await tick();
  });

  it('stays out of the way when the guide cannot load', async () => {
    const fixture = TestBed.createComponent(SetupCard);
    fixture.detectChanges();
    (await nextRequest(http, '/api/onboarding')).flush(null, {
      status: 500,
      statusText: 'Server Error',
    });
    await tick();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('section')).toBeNull();
  });
});
