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
    { id: 'account', state: 'done', derived: true },
    { id: 'portfolio', state: 'skipped', derived: false },
    { id: 'data', state: 'todo', derived: false },
    { id: 'follow', state: 'todo', derived: false },
    { id: 'alerts', state: 'todo', derived: false },
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
    expect(el.textContent).toContain('1 of 5 done, 1 skipped');
    expect(el.textContent).toContain('Next: choose what to watch');
    expect(el.querySelector('a')?.getAttribute('href')).toBe('/welcome');

    [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Hide'))!.click();
    const req = await nextRequest(http, '/api/onboarding', 'PUT');
    expect(req.request.body).toEqual({ dismissed: true });
    req.flush({ ...VIEW, dismissed: true, show: false });
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('section')).toBeNull();
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
