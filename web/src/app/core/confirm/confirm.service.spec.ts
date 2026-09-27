import { TestBed } from '@angular/core/testing';

import { ConfirmService, type ConfirmTicket } from './confirm.service';

describe('ConfirmService', () => {
  let confirm: ConfirmService;

  beforeEach(() => {
    confirm = TestBed.inject(ConfirmService);
  });

  it('passes the ticket and typed confirmation through to the dialog', async () => {
    const ticket: ConfirmTicket = {
      kind: 'Kill switch',
      live: true,
      lines: [{ label: 'Scope', value: 'Every portfolio' }],
    };
    const answer = confirm.confirm({
      title: 'Stop?',
      message: 'x',
      confirmLabel: 'Stop trading',
      ticket,
      typedConfirmation: 'STOP',
    });
    const req = confirm.request()!;
    expect(req.ticket).toBe(ticket);
    expect(req.typedConfirmation).toBe('STOP');
    req.resolve(true);
    await expect(answer).resolves.toBe(true);
    expect(confirm.request()).toBeNull();
  });

  it('a second request cancels the first instead of stacking', async () => {
    const first = confirm.confirm({ title: 'A', message: 'a', confirmLabel: 'Do A' });
    const second = confirm.confirm({ title: 'B', message: 'b', confirmLabel: 'Do B' });
    await expect(first).resolves.toBe(false);
    expect(confirm.request()!.title).toBe('B');
    confirm.request()!.resolve(false);
    await expect(second).resolves.toBe(false);
  });
});
