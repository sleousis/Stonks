import { TestBed } from '@angular/core/testing';

import { ColumnsPicker, visibleColumns } from './columns-picker';

const COLUMNS = [
  { key: 'started', label: 'Started', fixed: true },
  { key: 'strategy', label: 'Strategy' },
  { key: 'errors', label: 'Errors' },
];

describe('ColumnsPicker', () => {
  const KEY = 'test.columns';
  beforeEach(() => localStorage.removeItem(KEY));

  function render(defaultHidden: string[] = []) {
    const fixture = TestBed.createComponent(ColumnsPicker);
    const emitted: string[][] = [];
    fixture.componentRef.setInput('columns', COLUMNS);
    fixture.componentRef.setInput('storageKey', KEY);
    fixture.componentRef.setInput('defaultHidden', defaultHidden);
    fixture.componentInstance.hiddenChange.subscribe((h) => emitted.push(h));
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement, emitted };
  }

  it('offers every column but the fixed ones, the defaults hidden', () => {
    const { el, emitted } = render(['errors']);
    const labels = [...el.querySelectorAll('label')].map((l) => l.textContent?.trim());
    expect(labels).toEqual(['Strategy', 'Errors']);
    expect(emitted.at(-1)).toEqual(['errors']);
    expect(el.querySelector('summary')?.textContent).toContain('1');
  });

  it('hides and shows a column and remembers it in this browser', () => {
    const { fixture, el, emitted } = render();
    const box = el.querySelectorAll<HTMLInputElement>('input')[0];
    box.checked = false;
    box.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    TestBed.tick();
    expect(emitted.at(-1)).toEqual(['strategy']);
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual(['strategy']);
    expect(visibleColumns(COLUMNS, ['strategy']).map((c) => c.key)).toEqual(['started', 'errors']);
  });
});
