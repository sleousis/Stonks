import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { JournalNoteView, MeView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ToastService } from '../../core/notify/toast.service';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { CLIENT_ID, entry } from './tca.fixtures';
import { TradeOrderPage } from './trade-order.page';

const VIEWER: MeView = { ...TRADER, user_id: 'usr_v', role: 'viewer', scopes: ['read'] };

function note(overrides: Partial<JournalNoteView> = {}): JournalNoteView {
  return {
    id: 3,
    author: 'user:usr_1',
    note: 'Spread looked wide.',
    order_client_id: CLIENT_ID,
    created_at: '2026-09-26T08:00:00Z',
    updated_at: '2026-09-26T08:00:00Z',
    ...overrides,
  };
}

describe('TradeOrderPage', () => {
  let fixture: ComponentFixture<TradeOrderPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  function setup(me: MeView) {
    const canWrite = me.role !== 'viewer';
    TestBed.configureTestingModule({
      imports: [TradeOrderPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: SessionService,
          useValue: {
            me: signal(me),
            can: () => canWrite,
            whyNot: () => (canWrite ? null : 'Traders only.'),
            csrfToken: () => null,
            status: signal('signed-in'),
          },
        },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(TradeOrderPage);
    fixture.componentRef.setInput('clientId', CLIENT_ID);
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  afterEach(() => controller.verify());

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  async function load(notes: JournalNoteView[] = []) {
    const req = await nextRequest(controller, `/api/tca/orders/${encodeURIComponent(CLIENT_ID)}`);
    req.flush(entry({ notes }));
    await settle();
  }

  it('renders the order as a ticket with the shortfall broken down', async () => {
    setup(TRADER);
    await load();
    const ticket = el.querySelector('.ticket')!;
    expect(el.querySelector('h1')?.textContent).toContain('Buy 10 AAPL.US');
    expect(ticket.querySelector('app-side-tag')?.textContent).toContain('Buy');
    expect(ticket.textContent).toContain('$200.00');
    expect(ticket.textContent).toContain('$200.30');
    expect(ticket.textContent).toContain('Market move before the order arrived');
    expect(ticket.textContent).toContain('+5 bps');
    expect(ticket.textContent).toContain('+10 bps');
    expect(ticket.textContent).toContain('+17 bps');
    expect(ticket.textContent).toContain('$3.40');
    expect(ticket.textContent).toContain('Strategy signal');
    // The raw id is only secondary text, never the heading.
    expect(el.querySelector('h1')?.textContent).not.toContain(CLIENT_ID);
    expect(el.querySelector('.ticket-id')?.textContent).toContain(CLIENT_ID);
  });

  it('adds a note with the typed text', async () => {
    setup(TRADER);
    await load();
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    const area = el.querySelector<HTMLTextAreaElement>('#new-note')!;
    area.value = '  Filled late.  ';
    area.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    el.querySelector<HTMLButtonElement>('.add-note button[type=submit]')!.click();

    const req = await nextRequest(
      controller,
      `/api/tca/orders/${encodeURIComponent(CLIENT_ID)}/notes`,
      'POST',
    );
    expect(req.request.body).toEqual({ note: 'Filled late.' });
    req.flush(note({ id: 9, note: 'Filled late.' }), { status: 201, statusText: 'Created' });
    await settle();
    expect(el.querySelector('.note-list')?.textContent).toContain('Filled late.');
    expect(success).toHaveBeenCalledWith('Added the note.');
  });

  it('edits my own note with a PUT, and offers no edit on other notes', async () => {
    setup(TRADER);
    await load([note(), note({ id: 4, author: 'user:usr_other', note: 'Not mine.' })]);
    const edits = Array.from(el.querySelectorAll<HTMLButtonElement>('.note button')).filter(
      (b) => b.textContent?.trim() === 'Edit note',
    );
    expect(edits.length).toBe(1);
    edits[0].click();
    fixture.detectChanges();

    const area = el.querySelector<HTMLTextAreaElement>('#edit-3')!;
    area.value = 'Spread was wide, about 8 bps.';
    area.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    Array.from(el.querySelectorAll<HTMLButtonElement>('.note button'))
      .find((b) => b.textContent?.trim() === 'Save note')!
      .click();

    const req = await nextRequest(controller, '/api/tca/notes/3', 'PUT');
    expect(req.request.body).toEqual({ note: 'Spread was wide, about 8 bps.' });
    req.flush(note({ note: 'Spread was wide, about 8 bps.', updated_at: '2026-09-26T09:00:00Z' }));
    await settle();
    expect(el.querySelector('.note-list')?.textContent).toContain('about 8 bps');
    expect(el.querySelector('.note-list')?.textContent).toContain('(edited)');
  });

  it('shows a viewer the notes but no Add note', async () => {
    setup(VIEWER);
    await load([note()]);
    expect(el.textContent).toContain('Spread looked wide.');
    expect(el.querySelector('#new-note')).toBeNull();
    expect(el.textContent).not.toContain('Add note');
    expect(el.textContent).not.toContain('Edit note');
    expect(el.textContent).toContain('Traders only.');
  });
});
