import { ChangeDetectionStrategy, Component, viewChild } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { StatusChangeRequest } from '../../api/models';
import { tick } from '../../../testing/http';
import { dialogForm, fillDialog, goLiveReport } from '../../../testing/status-dialog';
import {
  OVERRIDE_MIN_REASON,
  type StatusChangeOptions,
  StatusChangeDialog,
} from './status-change-dialog';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusChangeDialog],
  template: `<app-status-change-dialog />`,
})
class Host {
  readonly dialog = viewChild.required(StatusChangeDialog);
}

describe('StatusChangeDialog', () => {
  let fixture: ComponentFixture<Host>;
  let el: HTMLElement;

  beforeEach(async () => {
    fixture = TestBed.createComponent(Host);
    await fixture.whenStable();
    el = fixture.nativeElement;
  });

  async function open(options: Partial<StatusChangeOptions> = {}) {
    const answer: Promise<StatusChangeRequest | null> = fixture.componentInstance.dialog().open({
      title: 'Stop mom?',
      message: 'It stops trading.',
      confirmLabel: 'Stop',
      minReason: 1,
      ...options,
    });
    await tick();
    fixture.detectChanges();
    return { answer };
  }

  const form = () => dialogForm(el)!;
  const hint = () => form().querySelector('.hint')!.textContent!.trim();
  const button = (label: string) =>
    [...form().querySelectorAll('button')].find((b) => b.textContent?.trim() === label)!;

  it('renders the title and message inside a labelled sheet', async () => {
    await open();
    const dialog = el.querySelector('dialog')!;
    const title = el.querySelector(`#${dialog.getAttribute('aria-labelledby')}`);
    expect(title?.textContent).toBe('Stop mom?');
    expect(el.querySelector(`#${dialog.getAttribute('aria-describedby')}`)?.textContent).toBe(
      'It stops trading.',
    );
  });

  it('needs a reason, then resolves to the body with override false', async () => {
    const { answer } = await open();
    expect(button('Stop').disabled).toBe(true);
    const submit = fillDialog(fixture, { reason: '  Losing money  ' });
    expect(submit.disabled).toBe(false);
    submit.click();
    await expect(answer).resolves.toEqual({ reason: 'Losing money', override: false });
    fixture.detectChanges();
    expect(dialogForm(el)).toBeNull();
  });

  it('resolves null on Cancel and on Escape', async () => {
    const { answer: first } = await open();
    button('Cancel').click();
    await expect(first).resolves.toBeNull();

    const { answer: second } = await open();
    el.querySelector('dialog')!.dispatchEvent(new Event('cancel', { cancelable: true }));
    await expect(second).resolves.toBeNull();
  });

  it('counts down the characters an override reason needs', async () => {
    await open({ override: true, minReason: OVERRIDE_MIN_REASON, typedConfirmation: 'override' });
    expect(form().textContent).toContain('Why override the gate?');
    fillDialog(fixture, { reason: 'too short' });
    expect(hint()).toContain('At least 20 characters, 11 more to go.');
    expect(hint()).not.toContain(';');
    fillDialog(fixture, { reason: 'a reason that is long enough' });
    expect(hint()).toBe('Kept in the audit history with your override.');
  });

  it('keeps confirm disabled until the typed phrase matches, and sends override true', async () => {
    const { answer } = await open({
      override: true,
      minReason: OVERRIDE_MIN_REASON,
      typedConfirmation: 'override',
    });
    const reason = 'a reason that is long enough';
    expect(fillDialog(fixture, { reason, typed: 'overrid' }).disabled).toBe(true);
    const submit = fillDialog(fixture, { reason, typed: 'override' });
    expect(submit.disabled).toBe(false);
    submit.click();
    await expect(answer).resolves.toEqual({ reason, override: true });
  });

  it('shows the go-live verdict with the failing checks by label', async () => {
    await open({ golive: goLiveReport('mom', false) });
    expect(form().textContent).toContain('Go-live check failed');
    expect(form().textContent).toContain('1 of 2 checks failed.');
    const failing = form().querySelector('[aria-label="Failing go-live checks"]')!;
    expect(failing.textContent).not.toContain('min_days');
    expect(failing.querySelectorAll('li')).toHaveLength(1);
  });

  it('shows a note when the go-live report is missing', async () => {
    await open({ goliveNote: 'Could not run the go-live check first.' });
    expect(form().textContent).toContain('Could not run the go-live check first.');
  });

  describe('hold to confirm', () => {
    it('replaces the submit button and ignores Enter in the form', async () => {
      const { answer } = await open({ hold: true, confirmLabel: 'Go live' });
      expect(form().querySelector('button[type="submit"]')).toBeNull();
      const hold = fillDialog(fixture, { reason: 'Ready' });
      expect(hold.textContent).toContain('Hold to go live');

      form().dispatchEvent(new Event('submit', { cancelable: true }));
      fixture.detectChanges();
      expect(dialogForm(el)).not.toBeNull();

      hold.click();
      fixture.detectChanges();
      hold.click();
      await expect(answer).resolves.toEqual({ reason: 'Ready', override: false });
    });

    it('stays disabled until there is a reason', async () => {
      await open({ hold: true, confirmLabel: 'Go live' });
      const hold = form().querySelector<HTMLButtonElement>('app-hold-button button')!;
      expect(hold.disabled).toBe(true);
    });

    it('falls back to typing when a phrase is also asked', async () => {
      await open({ hold: true, typedConfirmation: 'override' });
      expect(form().querySelector('app-hold-button')).toBeNull();
      expect(form().querySelector('button[type="submit"]')).not.toBeNull();
    });
  });

  it('cancels the pending request when opened again', async () => {
    const { answer: first } = await open();
    await open({ title: 'Second' });
    await expect(first).resolves.toBeNull();
    expect(form().textContent).toContain('Second');
  });
});
