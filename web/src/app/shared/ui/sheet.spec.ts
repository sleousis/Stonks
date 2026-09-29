import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { Sheet, TypedConfirm, typedMatches } from './sheet';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Sheet, TypedConfirm],
  template: `
    <app-sheet [open]="open()" labelledBy="t" describedBy="m" (dismiss)="dismissed = dismissed + 1">
      <form class="sheet-form">
        <h2 id="t">Title</h2>
        <p id="m" class="sheet-message">Message</p>
        <app-typed-confirm inputId="typed" phrase="override" [(value)]="typed" />
      </form>
    </app-sheet>
  `,
})
class Host {
  readonly open = signal(false);
  readonly typed = signal('');
  dismissed = 0;
}

describe('Sheet', () => {
  it('opens and closes its dialog with the open input', async () => {
    const fixture = TestBed.createComponent(Host);
    await fixture.whenStable();
    const dialog = (fixture.nativeElement as HTMLElement).querySelector('dialog')!;
    const showModal = vi.fn(() => dialog.setAttribute('open', ''));
    const close = vi.fn(() => dialog.removeAttribute('open'));
    Object.assign(dialog, { showModal, close });

    fixture.componentInstance.open.set(true);
    await fixture.whenStable();
    expect(showModal).toHaveBeenCalledTimes(1);
    expect(dialog.getAttribute('aria-labelledby')).toBe('t');
    expect(dialog.getAttribute('aria-describedby')).toBe('m');

    fixture.componentInstance.open.set(false);
    await fixture.whenStable();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('opens again when the browser closes it while the host keeps it open', async () => {
    // A second Escape or back gesture closes a dialog even when cancel is prevented.
    const fixture = TestBed.createComponent(Host);
    await fixture.whenStable();
    const dialog = (fixture.nativeElement as HTMLElement).querySelector('dialog')!;
    const showModal = vi.fn(() => dialog.setAttribute('open', ''));
    const close = vi.fn(() => dialog.removeAttribute('open'));
    Object.assign(dialog, { showModal, close });
    fixture.componentInstance.open.set(true);
    await fixture.whenStable();
    dialog.removeAttribute('open');
    dialog.dispatchEvent(new Event('close'));
    await fixture.whenStable();
    expect(dialog.hasAttribute('open')).toBe(true);
    expect(fixture.componentInstance.dismissed).toBe(1);
  });

  it('stays closed after a browser close the host agrees with', async () => {
    const fixture = TestBed.createComponent(Host);
    await fixture.whenStable();
    const dialog = (fixture.nativeElement as HTMLElement).querySelector('dialog')!;
    Object.assign(dialog, {
      showModal: vi.fn(() => dialog.setAttribute('open', '')),
      close: vi.fn(() => dialog.removeAttribute('open')),
    });
    fixture.componentInstance.open.set(true);
    await fixture.whenStable();
    dialog.removeAttribute('open');
    fixture.componentInstance.open.set(false);
    dialog.dispatchEvent(new Event('close'));
    await fixture.whenStable();
    expect(dialog.hasAttribute('open')).toBe(false);
  });

  it('reports Escape as dismiss instead of closing on its own', async () => {
    const fixture = TestBed.createComponent(Host);
    await fixture.whenStable();
    const dialog = (fixture.nativeElement as HTMLElement).querySelector('dialog')!;
    const event = new Event('cancel', { cancelable: true });
    dialog.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(fixture.componentInstance.dismissed).toBe(1);
  });

  it('binds the typed confirmation both ways and labels it with the phrase', async () => {
    const fixture = TestBed.createComponent(Host);
    await fixture.whenStable();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('label[for="typed"]')?.textContent).toContain('override');
    const input = el.querySelector<HTMLInputElement>('#typed')!;
    input.value = 'overr';
    input.dispatchEvent(new Event('input'));
    expect(fixture.componentInstance.typed()).toBe('overr');
  });
});

describe('typedMatches', () => {
  it('matches the exact phrase, ignoring surrounding spaces', () => {
    expect(typedMatches('override', ' override ')).toBe(true);
    expect(typedMatches('override', 'Override')).toBe(false);
    expect(typedMatches(undefined, '')).toBe(true);
  });
});
