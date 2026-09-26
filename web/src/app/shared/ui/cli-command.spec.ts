import { TestBed } from '@angular/core/testing';

import { tick } from '../../../testing/http';
import { CliCommand } from './cli-command';

describe('CliCommand', () => {
  it('shows the command and copies it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });

    const fixture = TestBed.createComponent(CliCommand);
    fixture.componentRef.setInput('command', 'stonks golive check momentum-v3');
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('code')?.textContent).toContain('stonks golive check momentum-v3');

    el.querySelector('button')!.click();
    await tick();
    fixture.detectChanges();
    expect(writeText).toHaveBeenCalledWith('stonks golive check momentum-v3');
    expect(el.querySelector('button')?.textContent).toContain('Copied');
  });
});
