import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { Page, Draft, RuleTemplateView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { nextRequest, tick } from '../../../testing/http';
import { RSI_TEMPLATE_SPEC, makeDraft } from '../../../testing/studio-fixtures';
import { StudioPage } from './studio.page';

const TEMPLATES: RuleTemplateView[] = [
  {
    id: 'rsi_mean_reversion',
    title: 'RSI mean reversion',
    description: 'Buy oversold names.',
    spec: RSI_TEMPLATE_SPEC,
  },
];

function page(items: Draft[]): Page<Draft> {
  return { items, total: items.length, limit: 100, offset: 0 };
}

describe('StudioPage', () => {
  let fixture: ComponentFixture<StudioPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;
  let navigate: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    confirm = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      imports: [StudioPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConfirmService, useValue: { confirm } },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    fixture = TestBed.createComponent(StudioPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => controller.verify());

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  async function load(drafts: Draft[], codeStrategies = true): Promise<void> {
    (await nextRequest(controller, '/api/studio/drafts')).flush(page(drafts));
    (await nextRequest(controller, '/api/studio/templates')).flush(TEMPLATES);
    (await nextRequest(controller, '/api/studio/capabilities')).flush({
      code_strategies: codeStrategies,
    });
    await settle();
  }

  function buttonNamed(text: string, root: ParentNode = el): HTMLButtonElement {
    const b = [...root.querySelectorAll('button')].find((x) => x.textContent?.trim() === text);
    if (!b) throw new Error(`no button ${text}`);
    return b;
  }

  it('lists drafts with their status', async () => {
    await load([
      makeDraft(),
      makeDraft({
        id: 'draft_2',
        name: 'Breakout',
        status: 'registered',
        registered_strategy_id: 'studio_breakout',
        strategy_status: 'active',
      }),
    ]);
    const cards = el.querySelectorAll('.draft');
    expect(cards).toHaveLength(2);
    expect(cards[0].textContent).toContain('RSI dip buyer');
    expect(cards[0].textContent).toContain('3 indicators');
    expect(cards[1].textContent).toContain('active');
  });

  it('shows an empty state that explains drafts', async () => {
    await load([]);
    expect(el.textContent).toContain('No drafts yet');
  });

  it('creates a draft from a template with the template spec and the chosen name', async () => {
    await load([]);
    buttonNamed('New draft', el.querySelector('app-page-header') as HTMLElement).click();
    fixture.detectChanges();

    const radio = [...el.querySelectorAll<HTMLInputElement>('input[type=radio]')].find(
      (r) => r.value === 't:rsi_mean_reversion',
    );
    radio?.click();
    fixture.detectChanges();
    const name = el.querySelector('#new-name') as HTMLInputElement;
    expect(name.value).toBe('RSI mean reversion');
    name.value = 'My RSI';
    name.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    buttonNamed('Create draft').click();
    const req = await nextRequest(controller, '/api/studio/drafts', 'POST');
    expect(req.request.body).toEqual({
      name: 'My RSI',
      kind: 'rule',
      spec: { ...RSI_TEMPLATE_SPEC, name: 'My RSI' },
    });
    req.flush(makeDraft({ id: 'draft_new', name: 'My RSI' }), {
      status: 201,
      statusText: 'Created',
    });
    await settle();
    expect(navigate).toHaveBeenCalledWith(['/studio', 'draft_new']);
  });

  it('explains how to enable code strategies when the API refuses them', async () => {
    await load([]);
    buttonNamed('New draft', el.querySelector('app-page-header') as HTMLElement).click();
    fixture.detectChanges();
    [...el.querySelectorAll<HTMLInputElement>('input[type=radio]')]
      .find((r) => r.value === 'code')
      ?.click();
    const name = el.querySelector('#new-name') as HTMLInputElement;
    name.value = 'Custom';
    name.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    buttonNamed('Create draft').click();
    const req = await nextRequest(controller, '/api/studio/drafts', 'POST');
    expect(req.request.body).toMatchObject({ kind: 'code', name: 'Custom' });
    req.flush(
      { title: 'Code strategies disabled', status: 403, detail: 'code strategies are disabled' },
      { status: 403, statusText: 'Forbidden' },
    );
    await settle();
    expect(el.textContent).toContain('Code strategies are turned off');
    expect(el.textContent).toContain('allow_code_strategies');
    expect(buttonNamed('Create draft').disabled).toBe(true);
  });

  it('turns the code option off up front when the server says so', async () => {
    await load([], false);
    buttonNamed('New draft', el.querySelector('app-page-header') as HTMLElement).click();
    fixture.detectChanges();
    const code = [...el.querySelectorAll<HTMLInputElement>('input[type=radio]')].find(
      (r) => r.value === 'code',
    );
    expect(code?.closest('label')?.textContent).toContain('Turned off on this server');
    code?.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Code strategies are turned off');
    expect(buttonNamed('Create draft').disabled).toBe(true);
  });

  it('keeps the code option on when the server allows it', async () => {
    await load([], true);
    buttonNamed('New draft', el.querySelector('app-page-header') as HTMLElement).click();
    fixture.detectChanges();
    [...el.querySelectorAll<HTMLInputElement>('input[type=radio]')]
      .find((r) => r.value === 'code')
      ?.click();
    fixture.detectChanges();
    expect(el.textContent).not.toContain('Code strategies are turned off');
  });

  it('deletes a draft only after confirming', async () => {
    await load([makeDraft()]);
    buttonNamed('Delete').click();
    await tick();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Delete RSI dip buyer?', tone: 'danger' }),
    );
    (await nextRequest(controller, '/api/studio/drafts/draft_abc123', 'DELETE')).flush(makeDraft());
    (await nextRequest(controller, '/api/studio/drafts')).flush(page([]));
    await settle();
    expect(el.textContent).toContain('No drafts yet');
  });

  it('renames a draft inline', async () => {
    await load([makeDraft()]);
    buttonNamed('Rename').click();
    fixture.detectChanges();
    const input = el.querySelector('.rename input') as HTMLInputElement;
    input.value = 'Dip buyer v2';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    buttonNamed('Rename', el.querySelector('.rename') as HTMLElement).click();
    const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123', 'PATCH');
    expect(req.request.body).toEqual({ name: 'Dip buyer v2' });
    req.flush(makeDraft({ name: 'Dip buyer v2' }));
    (await nextRequest(controller, '/api/studio/drafts')).flush(
      page([makeDraft({ name: 'Dip buyer v2' })]),
    );
    await settle();
    expect(el.textContent).toContain('Dip buyer v2');
  });
});
