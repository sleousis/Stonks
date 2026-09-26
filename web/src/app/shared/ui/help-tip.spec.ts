import { ChangeDetectionStrategy, Component } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { TableColumn } from './data-table/data-table';
import { DataTable } from './data-table/data-table';
import { HelpTip } from './help-tip';
import { StatTile } from './stat-tile';

interface Row {
  id: string;
  sharpe: number;
  cash: number;
}

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [HelpTip, StatTile, DataTable],
  template: `
    <app-help-tip id="direct" term="max_drawdown" />
    <app-help-tip id="unknown" term="Ticker" />
    <app-stat-tile id="tile-sharpe" label="Sharpe" value="1.20" />
    <app-stat-tile id="tile-cash" label="Cash" value="$10.00" />
    <app-stat-tile id="tile-off" label="Sharpe" value="1.20" [help]="false" />
    <app-stat-tile id="tile-explicit" label="Score" value="0.5" help="oos_score" />
    <app-data-table caption="Rows" [rows]="rows" [columns]="columns" />
  `,
})
class Host {
  rows: Row[] = [{ id: 'a', sharpe: 1, cash: 2 }];
  columns: TableColumn<Row>[] = [
    { key: 'id', label: 'Id' },
    { key: 'sharpe', label: 'Sharpe', format: 'number' },
    { key: 'cash', label: 'Cash', format: 'money' },
  ];
}

describe('HelpTip', () => {
  let el: HTMLElement;
  let fixture: ComponentFixture<Host>;

  beforeEach(async () => {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    await fixture.whenStable();
    el = fixture.nativeElement;
  });

  it('renders a labelled button that opens the one-line help and glossary link', async () => {
    const host = el.querySelector('#direct')!;
    const button = host.querySelector('button')!;
    expect(button.getAttribute('aria-label')).toBe('What is Max drawdown?');
    const panelId = button.getAttribute('popovertarget')!;
    const panel = host.querySelector<HTMLElement>(`[id="${panelId}"]`)!;
    expect(panel.hasAttribute('popover')).toBe(true);
    // Closed: no text, so the label next to it reads cleanly.
    expect(panel.textContent?.trim()).toBe('');

    panel.dispatchEvent(Object.assign(new Event('toggle'), { newState: 'open' }));
    fixture.detectChanges();
    await fixture.whenStable();
    expect(panel.textContent).toContain('largest fall from a peak');
    const link = panel.querySelector('a')!;
    expect(link.getAttribute('href')).toBe('/help/glossary#max_drawdown');
    expect(link.getAttribute('target')).toBeNull();

    panel.dispatchEvent(Object.assign(new Event('toggle'), { newState: 'closed' }));
    fixture.detectChanges();
    expect(panel.textContent?.trim()).toBe('');
  });

  it('opens the in-app glossary without reloading the page (UI-13)', async () => {
    const router = TestBed.inject(Router);
    const nav = vi.spyOn(router, 'navigateByUrl').mockResolvedValue(true);
    const panel = el.querySelector<HTMLElement>('#direct [popover]')!;
    panel.dispatchEvent(Object.assign(new Event('toggle'), { newState: 'open' }));
    fixture.detectChanges();
    const click = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
    panel.querySelector('a')!.dispatchEvent(click);
    expect(click.defaultPrevented).toBe(true);
    expect(nav).toHaveBeenCalledWith('/help/glossary#max_drawdown');
  });

  it('closes when the page scrolls (UI-33)', async () => {
    const panel = el.querySelector<HTMLElement>('#direct [popover]')!;
    panel.dispatchEvent(Object.assign(new Event('toggle'), { newState: 'open' }));
    fixture.detectChanges();
    expect(panel.textContent).toContain('largest fall');
    window.dispatchEvent(new Event('scroll'));
    fixture.detectChanges();
    expect(panel.textContent?.trim()).toBe('');
  });

  it('renders nothing for terms not in the glossary', () => {
    expect(el.querySelector('#unknown button')).toBeNull();
  });

  it('is added to metric stat tiles automatically, by label or explicit key', () => {
    expect(el.querySelector('#tile-sharpe app-help-tip button')).not.toBeNull();
    expect(el.querySelector('#tile-cash app-help-tip button')).toBeNull();
    expect(el.querySelector('#tile-off app-help-tip')).toBeNull();
    expect(el.querySelector('#tile-explicit app-help-tip button')?.getAttribute('aria-label')).toBe(
      'What is Out-of-sample score?',
    );
  });

  it('is added to metric table headers automatically', () => {
    const headers = Array.from(el.querySelectorAll('app-data-table th'));
    const withHelp = headers.filter((th) => th.querySelector('app-help-tip button'));
    expect(withHelp.map((th) => th.querySelector('.sort')?.textContent?.trim())).toEqual([
      'Sharpe',
    ]);
  });
});
