import { TestBed } from '@angular/core/testing';

import { ConfirmService } from '../../core/confirm/confirm.service';
import { ConfirmDialog } from './confirm-dialog';

describe('ConfirmDialog', () => {
  async function setup() {
    const fixture = TestBed.createComponent(ConfirmDialog);
    const confirm = TestBed.inject(ConfirmService);
    await fixture.whenStable();
    const el: HTMLElement = fixture.nativeElement;
    const button = (label: string) =>
      [...el.querySelectorAll<HTMLButtonElement>('button')].find(
        (b) => b.textContent?.trim() === label,
      )!;
    return { fixture, confirm, el, button };
  }

  it('resolves true on confirm and false on cancel', async () => {
    const { fixture, confirm, button } = await setup();

    const yes = confirm.confirm({ title: 'Retire x?', message: 'm', confirmLabel: 'Retire' });
    await fixture.whenStable();
    button('Retire').click();
    await expect(yes).resolves.toBe(true);

    const no = confirm.confirm({ title: 'Retire x?', message: 'm', confirmLabel: 'Retire' });
    await fixture.whenStable();
    button('Cancel').click();
    await expect(no).resolves.toBe(false);
  });

  it('requires the exact typed confirmation', async () => {
    const { fixture, confirm, el, button } = await setup();
    const answer = confirm.confirm({
      title: 'Promote momentum-v3?',
      message: 'It becomes active on the next tick.',
      confirmLabel: 'Promote',
      typedConfirmation: 'momentum-v3',
    });
    await fixture.whenStable();

    const input = el.querySelector<HTMLInputElement>('#confirm-typed')!;
    expect(el.querySelector('label[for="confirm-typed"]')?.textContent).toContain('momentum-v3');
    expect(button('Promote').disabled).toBe(true);

    input.value = 'momentum';
    input.dispatchEvent(new Event('input'));
    await fixture.whenStable();
    expect(button('Promote').disabled).toBe(true);

    input.value = 'momentum-v3';
    input.dispatchEvent(new Event('input'));
    await fixture.whenStable();
    expect(button('Promote').disabled).toBe(false);
    button('Promote').click();
    await expect(answer).resolves.toBe(true);
  });

  it('shows a money action as an order ticket', async () => {
    const { fixture, confirm, el, button } = await setup();
    const answer = confirm.confirm({
      title: 'Place this order?',
      message: 'It goes to the broker now.',
      confirmLabel: 'Place order',
      ticket: {
        side: 'buy',
        live: true,
        lines: [
          { label: 'Ticker', value: 'AAPL.US' },
          { label: 'Quantity', value: '10' },
        ],
      },
    });
    await fixture.whenStable();

    const ticket = el.querySelector('[aria-label="Order ticket"]')!;
    expect(ticket.querySelector('app-side-tag')?.textContent).toContain('Buy');
    expect(ticket.querySelector('app-mode-stamp')?.textContent).toContain('LIVE');
    const values = [...ticket.querySelectorAll('dd')].map((d) => d.textContent?.trim());
    expect(values).toEqual(['AAPL.US', '10']);
    expect(ticket.querySelector('dd')?.classList).toContain('num');

    button('Place order').click();
    await expect(answer).resolves.toBe(true);
  });

  it('shows a paper ticket without a side', async () => {
    const { fixture, confirm, el } = await setup();
    void confirm.confirm({
      title: 't',
      message: 'm',
      confirmLabel: 'Go',
      ticket: { live: false, lines: [{ label: 'Portfolio', value: 'pf_default' }] },
    });
    await fixture.whenStable();
    expect(el.querySelector('app-mode-stamp')?.textContent).toContain('PAPER');
    expect(el.querySelector('app-side-tag')).toBeNull();
  });

  it('has no ticket for plain confirmations', async () => {
    const { fixture, confirm, el } = await setup();
    void confirm.confirm({ title: 't', message: 'm', confirmLabel: 'Go' });
    await fixture.whenStable();
    expect(el.querySelector('[aria-label="Order ticket"]')).toBeNull();
  });

  it('cancels a pending request when a new one arrives', async () => {
    const { confirm } = await setup();
    const first = confirm.confirm({ title: 'a', message: 'a', confirmLabel: 'A' });
    void confirm.confirm({ title: 'b', message: 'b', confirmLabel: 'B' });
    await expect(first).resolves.toBe(false);
  });

  it('treats Escape as cancel', async () => {
    const { fixture, confirm, el } = await setup();
    const answer = confirm.confirm({ title: 'a', message: 'a', confirmLabel: 'A' });
    await fixture.whenStable();
    el.querySelector('dialog')!.dispatchEvent(new Event('cancel', { cancelable: true }));
    await expect(answer).resolves.toBe(false);
  });
});
