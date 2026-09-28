import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { nextRequest, tick } from '../../../testing/http';
import { JournalTradePage } from './journal-trade.page';
import { detail } from './journal.fixtures';

describe('JournalTradePage', () => {
  let fixture: ComponentFixture<JournalTradePage>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  function create(canWrite: boolean) {
    TestBed.configureTestingModule({
      imports: [JournalTradePage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: SessionService,
          useValue: {
            me: signal(null),
            can: () => canWrite,
            whyNot: () => (canWrite ? null : 'Traders only.'),
            csrfToken: () => null,
            status: signal('signed-in'),
          },
        },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(JournalTradePage);
    fixture.componentRef.setInput('tradeId', '7');
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  async function load(): Promise<void> {
    (await nextRequest(controller, '/api/journal/trades/7')).flush(detail());
    (await nextRequest(controller, '/api/journal/playbooks')).flush({
      items: [],
      total: 0,
      limit: 500,
      offset: 0,
    });
    (await nextRequest(controller, '/api/journal/labels')).flush({
      tags: ['earnings'],
      mistakes: [],
    });
    await settle();
  }

  it('shows the legs with R, excursions and the stop it came from', async () => {
    create(true);
    await load();
    const text = el.textContent ?? '';
    expect(text).toContain('Long AAPL.US');
    expect(text).toContain('+2.0R');
    expect(text).toContain('75.0%');
    expect(text).toContain('the trade plan on the order');
    expect(el.querySelector<HTMLInputElement>('#tags')!.value).toBe('earnings');
  });

  it('saves the review with parsed labels and the plan flag', async () => {
    create(true);
    await load();
    const mistakes = el.querySelector<HTMLInputElement>('#mistakes')!;
    mistakes.value = 'Late entry, sized too big';
    mistakes.dispatchEvent(new Event('input'));
    const broke = Array.from(el.querySelectorAll<HTMLButtonElement>('[role=radio]')).find(
      (b) => b.textContent?.trim() === 'Broke it',
    )!;
    broke.click();
    fixture.detectChanges();
    el.querySelector<HTMLFormElement>('form.review')!.dispatchEvent(new Event('submit'));
    const put = await nextRequest(controller, '/api/journal/trades/7/review', 'PUT');
    expect(put.request.body).toEqual({
      tags: ['earnings'],
      mistakes: ['late entry', 'sized too big'],
      playbook_id: null,
      followed_plan: false,
      review: null,
    });
    put.flush({});
    await settle();
    for (const r of controller.match(() => true))
      r.flush(r.request.url.endsWith('/7') ? detail() : {});
  });

  it('reads only without portfolio.manage', async () => {
    create(false);
    await load();
    expect(el.querySelector('form.review')).toBeNull();
    expect(el.querySelector('app-permission-note')).not.toBeNull();
  });
});
