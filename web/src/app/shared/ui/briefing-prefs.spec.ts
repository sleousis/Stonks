import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { nextRequest, tick } from '../../../testing/http';
import { BRIEFING_KINDS, BriefingPrefs } from './briefing-prefs';

describe('BriefingPrefs', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [BriefingPrefs],
      providers: [...provideApi(), provideHttpClientTesting()],
    });
    vi.spyOn(TestBed.inject(SessionService), 'can').mockReturnValue(true);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('names both briefings in plain words', () => {
    expect(BRIEFING_KINDS.map((k) => k.label)).toEqual(['Before the open', 'After the close']);
  });

  it('turns a briefing on and says when the install has them off', async () => {
    const fixture = TestBed.createComponent(BriefingPrefs);
    fixture.detectChanges();
    (await nextRequest(http, '/api/assistant/briefings/prefs')).flush({
      available: false,
      pre_open: false,
      post_close: false,
    });
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Briefings are off on this install.');
    const [first] = el.querySelectorAll<HTMLInputElement>('input[type=checkbox]');
    first.checked = true;
    first.dispatchEvent(new Event('change'));
    const put = await nextRequest(http, '/api/assistant/briefings/prefs', 'PUT');
    expect(put.request.body).toEqual({ pre_open: true, post_close: false });
    put.flush({ available: false, pre_open: true, post_close: false });
    await tick();
  });
});
