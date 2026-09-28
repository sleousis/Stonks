import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component, input } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import type { DataCoverage, MeView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { DataPlanNote, type PaidDataKind } from './data-plan-note';

@Component({
  imports: [DataPlanNote],
  template: `<app-data-plan-note [kind]="kind()" />`,
})
class Host {
  readonly kind = input<PaidDataKind>('calendars');
}

const NONE: DataCoverage = { fundamentals: false, calendars: false, news: false, options: false };

describe('DataPlanNote', () => {
  async function render(me: MeView, coverage: DataCoverage, kind: PaidDataKind = 'calendars') {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    const controller = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(me);
    await loading;
    const fixture = TestBed.createComponent(Host);
    fixture.componentRef.setInput('kind', kind);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/market/data-coverage')).flush(coverage);
    await tick();
    fixture.detectChanges();
    controller.verify();
    return fixture.nativeElement as HTMLElement;
  }

  it('tells a trader the page needs a data plan and who can add it', async () => {
    const el = await render(TRADER, NONE);
    expect(el.textContent).toContain('Needs a data plan');
    expect(el.textContent).toContain('calendars come with a paid data plan');
    expect(el.textContent).toContain('Your admin can add a plan that includes it.');
    expect(el.querySelector('a')).toBeNull();
  });

  it('tells an admin what to do and where', async () => {
    const el = await render(ADMIN, NONE, 'news');
    expect(el.textContent).toContain('News and its mood scores come with a paid data plan');
    expect(el.querySelector('a')?.getAttribute('href')).toBe('/data');
  });

  it('renders nothing when the data is stored', async () => {
    const el = await render(TRADER, { ...NONE, options: true }, 'options');
    expect(el.querySelector('.plan-note')).toBeNull();
  });
});
