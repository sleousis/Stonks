import { computed, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { HaltsService } from '../../api/halts.service';
import { SessionService } from '../../core/auth/session.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { book } from '../../../testing/portfolio-fixtures';
import { KillSheet } from './kill-sheet';

describe('KillSheet', () => {
  it('keeps the chosen scope when the portfolio list is read again', async () => {
    const options = signal([book({ id: 'pf_1', name: 'Main', is_default: true })]);
    TestBed.configureTestingModule({
      providers: [
        { provide: HaltsService, useValue: { kill: vi.fn() } },
        {
          provide: PortfolioContextService,
          useValue: {
            options,
            current: computed(() => options()[0] ?? null),
          },
        },
        { provide: SessionService, useValue: { can: () => false, me: () => null } },
      ],
    });
    const fixture = TestBed.createComponent(KillSheet);
    fixture.componentRef.setInput('open', true);
    await fixture.whenStable();
    const el = fixture.nativeElement as HTMLElement;
    const all = el.querySelector<HTMLInputElement>('input[value="user"]')!;
    all.click();
    await fixture.whenStable();
    expect(all.checked).toBe(true);
    // A re-read brings the same portfolio as a new object.
    options.set([book({ id: 'pf_1', name: 'Main', is_default: true })]);
    await fixture.whenStable();
    expect(el.querySelector<HTMLInputElement>('input[value="user"]')!.checked).toBe(true);
  });
});
