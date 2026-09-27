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
      title="Glossary"
      description="What each trading word and figure in the console means, in plain words."
    />

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
      <p class="empty">No term matches "{{ query() }}". Try a shorter word.</p>
    } @else {
      <div #list>
        @for (g of groups(); track g.title) {
          <section class="group" [attr.aria-labelledby]="'glossary-' + $index">
            <h2 [id]="'glossary-' + $index">{{ g.title }}</h2>
            <dl class="terms">
              @for (e of g.entries; track e.key) {
                <div class="term" [id]="e.key" tabindex="-1" [class.target]="e.key === target()">
                  <dt>{{ e.term }}</dt>
                  <dd>{{ e.short }}</dd>
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
    .search {
      max-width: 28rem;
    }
    .count {
      margin: var(--space-2) 0 var(--space-4);
      font-size: var(--text-sm);
    }
    .group + .group {
      margin-top: var(--space-6);
    }
    .group h2 {
      margin-bottom: var(--space-3);
      font-size: var(--text-lg);
    }
    .terms {
      display: grid;
      gap: var(--space-2);
      margin: 0;
    }
    .term {
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      background: var(--color-surface);
      scroll-margin-top: var(--space-8);
    }
    .term.target {
      border-color: var(--color-focus);
      box-shadow: inset 3px 0 0 var(--color-focus);
    }
    dt {
      font-weight: var(--weight-semibold);
    }
    dd {
      margin: var(--space-1) 0 0;
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .aliases {
      font-size: var(--text-xs);
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
