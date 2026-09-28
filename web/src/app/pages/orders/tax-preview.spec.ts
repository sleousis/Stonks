import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { tick } from '../../../testing/http';
import { provideFakeTax, taxPreview } from '../../../testing/fake-tax';
import type { TaxPreviewView } from '../../api/models';
import { TaxPreviewPanel, holdingLabel, worthShowing } from './tax-preview';

describe('TaxPreviewPanel', () => {
  let fixture: ComponentFixture<TaxPreviewPanel>;
  let preview: ReturnType<typeof vi.fn>;

  function setup(answer: TaxPreviewView) {
    preview = vi.fn(async () => answer);
    TestBed.configureTestingModule({ providers: [provideFakeTax({ preview } as never)] });
    fixture = TestBed.createComponent(TaxPreviewPanel);
  }

  async function ask(q: unknown) {
    fixture.componentRef.setInput('question', q);
    fixture.detectChanges();
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
    return fixture.nativeElement as HTMLElement;
  }

  it('shows the lots a sell closes, the gain and the tax', async () => {
    setup(
      taxPreview({
        lots: [
          {
            open_fill_id: 3,
            kind: 'long',
            quantity: 10,
            acquired: '2024-01-10',
            holding_period: 'long',
            cost_basis: 500,
            proceeds: 1000,
            gain: 500,
            wash_sale_disallowed: 0,
          },
        ],
        proceeds: 1000,
        realized_gain: 500,
        long_term_gain: 500,
        estimated_tax: 75,
        after_tax_proceeds: 925,
        year_tax_change: 75,
      }),
    );
    const el = await ask({ ticker: 'aaa.us', side: 'sell', quantity: 10, price: 100 });
    expect(preview).toHaveBeenCalledWith('AAA.US', 'sell', 10, 100, null);
    const text = el.textContent ?? '';
    expect(text).toContain('Tax preview');
    expect(text).toContain('Long term');
    expect(text).toContain('$75.00');
    expect(text).toContain('$925.00');
    expect(text).toContain('+$75.00');
  });

  it('warns of a wash sale even when no lot closes', async () => {
    setup(taxPreview({ side: 'buy', wash_sale_warning: 'you sold AAA.US at a loss' }));
    const el = await ask({ ticker: 'AAA.US', side: 'buy', quantity: 1 });
    expect(el.querySelector('[role="status"]')?.textContent).toContain('at a loss');
    expect(el.querySelector('table')).toBeNull();
  });

  it('shows nothing for a trade with nothing to say, or no question', async () => {
    setup(taxPreview({ side: 'buy' }));
    await ask(null);
    expect(preview).not.toHaveBeenCalled();
    const el = await ask({ ticker: 'AAA.US', side: 'buy', quantity: 1 });
    expect(el.textContent?.trim()).toBe('');
  });

  it('asks once for the same question built again', async () => {
    setup(taxPreview());
    await ask({ ticker: 'AAA.US', side: 'sell', quantity: 1 });
    await ask({ ticker: 'AAA.US', side: 'sell', quantity: 1 });
    expect(preview).toHaveBeenCalledTimes(1);
  });

  it('labels holding periods and knows when to show', () => {
    expect(holdingLabel('long')).toBe('Long term');
    expect(holdingLabel('short')).toBe('Short term');
    expect(worthShowing(taxPreview())).toBe(false);
    expect(worthShowing(taxPreview({ wash_sale_warning: 'x' }))).toBe(true);
  });
});
