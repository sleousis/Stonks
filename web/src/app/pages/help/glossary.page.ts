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
    <app-page-header
      title="Help"
      description="How Stonks works, and what each word and figure in the console means."
    />

    <!-- The product in five sentences (F21), in the words of docs/design/vocabulary.md. -->
    <section class="panel intro" aria-labelledby="how-title">
      <div class="panel-head">
        <h2 id="how-title">How Stonks works</h2>
      </div>
      <ol class="panel-body steps prose">
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
          approving each trade yourself or letting it trade automatically, always within the amount
          you set.
        </li>
        <li>
          <strong>Today</strong> shows what your money is doing, what your strategies want next and
          anything waiting for you.
        </li>
        <li>
          If anything looks wrong, <strong>Stop trading</strong> at the top of every page stops new
          orders at once, and nothing starts again until you say so.
        </li>
      </ol>
    </section>

    <div class="field search">
      <label for="glossary-filter">Find a term</label>
      <input
        id="glossary-filter"
        class="input"
        type="search"
        autocomplete="off"
        [value]="query()"
        (input)="query.set($any($event.target).value)"
      />
    </div>

    <p class="count muted" role="status">
      {{ entries().length === 1 ? '1 term' : entries().length + ' terms' }}
    </p>

    @if (entries().length === 0) {
      <p class="empty callout" data-tone="neutral">
        No term matches "{{ query() }}". Try a shorter word.
      </p>
    } @else {
      <div #list>
        @for (g of groups(); track g.title) {
          <section class="group panel" [attr.aria-labelledby]="'glossary-' + $index">
            <div class="panel-head">
              <h2 [id]="'glossary-' + $index">{{ g.title }}</h2>
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
                    <dd class="aliases muted">Also shown as {{ e.aliases.join(', ') }}</dd>
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
    .intro {
      margin-bottom: var(--space-5);
    }
    .steps {
      display: grid;
      gap: var(--space-2);
      margin: 0;
      padding-left: calc(var(--space-4) + 1.25rem);
      color: var(--color-ink-2);
    }
    .steps ::marker {
      font-family: var(--font-mono);
      color: var(--color-ink-3);
    }
    .search {
      max-width: 28rem;
    }
    .count {
      margin: var(--space-2) 0 var(--space-4);
      font-size: var(--text-sm);
      font-variant-numeric: tabular-nums slashed-zero;
    }
    .empty {
      margin: 0;
    }
    // One panel per group, the terms a definition list on hairlines.
    .group + .group {
      margin-top: var(--space-4);
    }
    .terms {
      display: grid;
      margin: 0;
      padding: 0 var(--space-4);
    }
    .term {
      padding: var(--space-3) 0;
      scroll-margin-top: var(--space-8);
    }
    .term + .term {
      border-top: 1px solid var(--color-border);
    }
    // The term a link pointed at: an accent rule at the panel edge.
    .term.target {
      margin-inline: calc(-1 * var(--space-4));
      padding-inline: calc(var(--space-4) - 2px) var(--space-4);
      border-left: 2px solid var(--color-focus);
      background: var(--color-accent-soft);
    }
    .term:focus-visible {
      outline-offset: -2px;
    }
    dt {
      font-size: var(--text-md);
      font-weight: var(--weight-semibold);
    }
    dd {
      max-width: 70ch;
      margin: var(--space-1) 0 0;
      font-size: var(--text-md);
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .aliases {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .example {
      font-size: var(--text-sm);
    }
    @media (max-width: 767.98px) {
      .intro {
        margin-bottom: var(--space-4);
      }
      .group + .group {
        margin-top: var(--space-3);
      }
      .terms {
        padding: 0 var(--space-3);
      }
      .term.target {
        margin-inline: calc(-1 * var(--space-3));
        padding-inline: calc(var(--space-3) - 2px) var(--space-3);
      }
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
