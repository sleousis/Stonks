import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';

import { ApiError } from '../http/api-error';
import { StepUpDialog } from './step-up-dialog';
import { type StepUpRequest, StepUpService } from './step-up.service';

describe('StepUpDialog', () => {
  const request = signal<StepUpRequest | null>(null);
  const service = {
    request: request.asReadonly(),
    submit: vi.fn(async () => undefined),
    cancel: vi.fn(() => request.set(null)),
  };

  beforeEach(() => {
    request.set(null);
    service.submit.mockReset();
    service.cancel.mockClear();
    TestBed.configureTestingModule({ providers: [{ provide: StepUpService, useValue: service }] });
  });

  function render() {
    const fixture = TestBed.createComponent(StepUpDialog);
    fixture.detectChanges();
    return fixture;
  }

  function type(el: HTMLElement, selector: string, value: string) {
    const input = el.querySelector<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  it('shows the reason and sends the app code', async () => {
    const fixture = render();
    request.set({ reason: 'Turn on auto for momentum' });
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Turn on auto for momentum');

    type(el, '#step-up-code', ' 123456 ');
    fixture.detectChanges();
    el.querySelector('form')!.dispatchEvent(new Event('submit'));
    await fixture.whenStable();
    expect(service.submit).toHaveBeenCalledWith({ code: '123456' });
  });

  it('sends a recovery code when switched', async () => {
    const fixture = render();
    request.set({ reason: 'x' });
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    el.querySelector<HTMLButtonElement>('.switch')!.click();
    fixture.detectChanges();
    type(el, '#step-up-recovery', 'abcd-efgh');
    fixture.detectChanges();
    el.querySelector('form')!.dispatchEvent(new Event('submit'));
    await fixture.whenStable();
    expect(service.submit).toHaveBeenCalledWith({ recovery_code: 'abcd-efgh' });
  });

  it('shows a wrong code inline', async () => {
    service.submit.mockRejectedValue(
      new ApiError(401, 'x', 'That did not match. Try again.', [], 'invalid_credentials'),
    );
    const fixture = render();
    request.set({ reason: 'x' });
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    type(el, '#step-up-code', '000000');
    fixture.detectChanges();
    el.querySelector('form')!.dispatchEvent(new Event('submit'));
    await fixture.whenStable();
    fixture.detectChanges();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('did not match');
  });

  it('cancels', () => {
    const fixture = render();
    request.set({ reason: 'x' });
    fixture.detectChanges();
    const buttons = [...fixture.nativeElement.querySelectorAll('button')] as HTMLButtonElement[];
    buttons.find((b) => b.textContent?.trim() === 'Cancel')!.click();
    expect(service.cancel).toHaveBeenCalled();
  });
});
