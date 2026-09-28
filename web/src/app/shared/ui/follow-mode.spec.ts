import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { SubscriptionMode } from '../../api/subscriptions.service';
import { FollowMode } from './follow-mode';

@Component({
  imports: [FollowMode],
  template: `<app-follow-mode
    label="Mode for Momentum"
    [value]="value()"
    [locked]="locked()"
    (changed)="picked.push($event)"
  />`,
})
class Host {
  readonly value = signal<SubscriptionMode>('paper');
  readonly locked = signal<SubscriptionMode[]>(['approve', 'auto']);
  readonly picked: SubscriptionMode[] = [];
}

describe('FollowMode', () => {
  function render() {
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    return { fixture, el, select: el.querySelector('select')! };
  }

  it('lists the four follow modes in the vocabulary words, labelled (M8)', () => {
    const { el, select } = render();
    const options = [...select.options].map((o) => o.textContent?.trim());
    expect(options).toEqual([
      'Alerts only',
      'Paper',
      'Approve each trade (locked)',
      'Automatic (locked)',
    ]);
    expect(select.value).toBe('paper');
    expect(el.querySelector(`label[for="${select.id}"]`)?.textContent).toBe('Mode for Momentum');
  });

  it('keeps locked modes shown but not pickable', () => {
    const { select } = render();
    expect([...select.options].map((o) => o.disabled)).toEqual([false, false, true, true]);
  });

  it('emits a pick and stays on the stored mode until the parent changes it', () => {
    const { fixture, select } = render();
    select.value = 'notify';
    select.dispatchEvent(new Event('change'));
    expect(fixture.componentInstance.picked).toEqual(['notify']);
    // Refused (a cancelled ticket): the control still shows the stored mode.
    expect(select.value).toBe('paper');
    fixture.componentInstance.value.set('notify');
    fixture.detectChanges();
    expect(select.value).toBe('notify');
  });
});
