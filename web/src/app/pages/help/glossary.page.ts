import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  afterNextRender,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { ActivatedRoute } from '@angular/router';

import { GLOSSARY, GLOSSARY_GROUPS } from '../../core/help/glossary';
import { PageHeader } from '../../shared/ui/page-header';

/**
 * Every term the console explains, in one list with an anchor per term
 * (`/help/glossary#sharpe`). Help tips link here. Built from
 * `core/help/glossary.ts`, so a new metric shows up without touching this page.
 */
@Component({
  selector: 'app-glossary-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader],
  template: `
    <app-page-header title="Help" [description]="standfirst" />

    <!-- Search on top: it filters the glossary below as you type. -->
    <div class="finder">
      <div class="field search">
        <label for="glossary-filter">Find a term</label>
        <div class="search-box">
          <svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <circle cx="7" cy="7" r="4.5" stroke="currentColor" stroke-width="1.6" />
            <path
              d="m10.5 10.5 3 3"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linecap="round"
            />
          </svg>
          <input
            id="glossary-filter"
            class="input"
            type="search"
            autocomplete="off"
            placeholder="Sharpe, paper, drawdown"
            [value]="query()"
            (input)="query.set($any($event.target).value)"
          />
        </div>
      </div>
      <p class="count muted" role="status">
        <span class="num">{{ entries().length }}</span>
        {{ entries().length === 1 ? 'term' : 'terms' }}
      </p>
    </div>

    @if (!query().trim()) {
      <!-- The product in five sentences (F21), in the words of docs/design/vocabulary.md. -->
      <section class="page-section intro" aria-labelledby="how-title">
        <div class="section-head">
          <h2 id="how-title">How Stonks works</h2>
        </div>
        <ol class="steps">
          <li>
            Stonks runs <strong>strategies</strong>: rules that read prices after each close and
            decide what to buy and sell.
          </li>
          <li>
            You <strong>follow</strong> a strategy in one of your <strong>portfolios</strong>, first
            as alerts only or on paper, so you see how it does without risk.
          </li>
          <li>
            After enough paper days you may let it trade <strong>real money</strong> at your broker,
            approving each trade yourself or letting it trade automatically, always within the
            amount you set.
          </li>
          <li>
            <strong>Today</strong> shows what your money is doing, what your strategies want next
            and anything waiting for you.
          </li>
          <li>
            If anything looks wrong, <strong>Stop trading</strong> at the top of every page stops
            new orders at once, and nothing starts again until you say so.
          </li>
        </ol>
      </section>
    }

    @if (entries().length === 0) {
      <p class="empty callout" data-tone="neutral">
        No term matches "{{ query() }}". Try a shorter word.
      </p>
    } @else {
      <nav class="filter-pills jump" aria-label="Glossary sections">
        @for (g of groups(); track g.title) {
          <a
            class="filter-pill"
            [href]="'/help/glossary#glossary-' + $index"
            (click)="jump($event, 'glossary-' + $index)"
            >{{ g.title }} <span class="count">{{ g.entries.length }}</span></a
          >
        }
      </nav>
      <div #list class="groups">
        @for (g of groups(); track g.title) {
          <section class="group page-section" [attr.aria-labelledby]="'glossary-' + $index">
            <div class="section-head">
              <h2 [id]="'glossary-' + $index" tabindex="-1">
                {{ g.title }} <span class="count">{{ g.entries.length }}</span>
              </h2>
            </div>
            <dl class="terms">
              @for (e of g.entries; track e.key) {
                <div class="term" [id]="e.key" tabindex="-1" [class.target]="e.key === target()">
                  <dt>{{ e.term }}</dt>
                  <dd>{{ e.short }}</dd>
                  @if (e.example) {
                    <dd class="example">For example: {{ e.example }}</dd>
                  }
                  @if (e.aliases.length) {
                    <dd class="aliases">Also shown as {{ e.aliases.join(', ') }}</dd>
                  }
                </div>
              }
            </dl>
          </section>
        }
      </div>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
    }
    .finder {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      justify-content: space-between;
      gap: var(--space-3) var(--space-6);
      margin-bottom: var(--section-gap);
    }
    .search {
      flex: 1 1 22rem;
      max-width: 36rem;
    }
    .search-box {
      position: relative;
    }
    .search-box svg {
      position: absolute;
      top: 50%;
      left: var(--space-3);
      width: 16px;
      height: 16px;
      color: var(--muted);
      transform: translateY(-50%);
      pointer-events: none;
    }
    .search-box .input {
      min-height: 44px;
      padding-left: 36px;
      font-size: var(--text-body);
    }
    .count {
      margin: 0 0 var(--space-3);
      font-size: var(--text-sm);
    }
    .intro {
      margin-bottom: var(--section-gap);
    }
    /* Five sentences on a numbered strip, hairlines between them. */
    .steps {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
      margin: 0;
      padding: 0;
      list-style: none;
      counter-reset: step;
      overflow: hidden;
    }
    .steps li {
      margin: -1px 0 0 -1px;
      padding: var(--space-4) var(--space-5) var(--space-4) var(--space-4);
      border-top: 1px solid var(--rule);
      border-left: 1px solid var(--rule);
      color: var(--ink-2);
      font-size: var(--text-md);
      counter-increment: step;
    }
    .steps li::before {
      content: counter(step, decimal-leading-zero);
      display: block;
      margin-bottom: var(--space-2);
      color: var(--accent);
      font-family: var(--font-mono);
      font-size: var(--text-xs);
      font-weight: var(--weight-semibold);
    }
    .steps strong {
      color: var(--ink);
    }
    .empty {
      margin: 0;
    }
    .jump {
      margin-bottom: var(--space-6);
    }
    .groups {
      display: grid;
      gap: var(--section-gap);
    }
    .group h2 {
      outline: none;
      scroll-margin-top: calc(var(--chrome-h) + var(--rail-h) + var(--space-4));
    }
    /* Two columns: the term on the left, what it means on the right. */
    .terms {
      margin: 0;
    }
    .term {
      display: grid;
      gap: var(--space-1) var(--space-8);
      padding: var(--space-4) 0;
      border-bottom: 1px solid var(--rule);
      scroll-margin-top: calc(var(--chrome-h) + var(--rail-h) + var(--space-4));
    }
    @include bp.from-tablet {
      .term {
        grid-template-columns: minmax(0, 15rem) minmax(0, 1fr);
      }
      .term dt {
        grid-row: 1 / span 3;
      }
      .term dd {
        grid-column: 2;
      }
    }
    .term:last-child {
      border-bottom: 0;
    }
    /* The term a link pointed at: an accent edge and the soft accent fill. */
    .term.target {
      padding-inline: var(--space-4);
      border-radius: var(--r-sheet);
      border-bottom-color: transparent;
      box-shadow: inset 3px 0 0 var(--accent);
      background: var(--accent-soft);
    }
    .term:focus-visible {
      outline-offset: -2px;
    }
    dt {
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
    }
    dd {
      max-width: 68ch;
      margin: 0;
      color: var(--ink-2);
      font-size: var(--text-md);
      overflow-wrap: anywhere;
    }
    .example {
      font-size: var(--text-sm);
    }
    .aliases {
      color: var(--muted);
      font-size: var(--text-sm);
    }
  `,
})
export class GlossaryPage {
  private readonly route = inject(ActivatedRoute);
  private readonly list = viewChild<ElementRef<HTMLElement>>('list');

  protected readonly query = signal('');
  protected readonly target = signal<string | null>(this.route.snapshot.fragment);

  private readonly all = GLOSSARY_GROUPS.map((g) => ({
    title: g.title,
    entries: [...g.keys]
      .map((key) => ({
        key,
        term: GLOSSARY[key].term,
        short: GLOSSARY[key].short,
        example: GLOSSARY[key].example ?? null,
        aliases: (GLOSSARY[key].aliases ?? []).filter((a) => /\s|[A-Z]/.test(a)),
      }))
      .sort((a, b) => a.term.localeCompare(b.term)),
  }));

  /** The sections with the terms that match the filter; empty sections drop out. */
  protected readonly groups = computed(() => {
    const q = this.query().trim().toLowerCase();
    if (!q) return this.all;
    return this.all
      .map((g) => ({
        title: g.title,
        entries: g.entries.filter((e) =>
          [e.term, e.short, ...e.aliases].some((t) => t.toLowerCase().includes(q)),
        ),
      }))
      .filter((g) => g.entries.length > 0);
  });

  protected readonly entries = computed(() => this.groups().flatMap((g) => g.entries));

  /** One sentence, counted from the glossary. */
  protected readonly standfirst = `How Stonks works, and what each of the ${this.all.reduce(
    (n, g) => n + g.entries.length,
    0,
  )} words and figures in the console means.`;

  /** Jump to a glossary section and put focus on its heading. */
  protected jump(event: Event, id: string): void {
    event.preventDefault();
    const heading = this.list()?.nativeElement.querySelector<HTMLElement>(`#${id}`);
    if (!heading) return;
    const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    heading.scrollIntoView?.({ behavior: still ? 'auto' : 'smooth', block: 'start' });
    heading.focus({ preventScroll: true });
  }

  constructor() {
    // Bring the linked term into view and focus it, so keyboard and screen
    // reader users land on it too.
    afterNextRender(() => {
      const key = this.target();
      if (!key) return;
      const el = this.list()?.nativeElement.querySelector<HTMLElement>(`[id="${CSS.escape(key)}"]`);
      el?.scrollIntoView?.({ block: 'start' });
      el?.focus({ preventScroll: true });
    });
  }
}
