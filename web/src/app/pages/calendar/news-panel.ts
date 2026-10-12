import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { CalendarsService, type NewsQuery } from '../../api/calendars.service';
import type { NewsItem } from '../../api/models';
import { formatAgo, formatDate, formatNumber } from '../../core/format/format';
import { DataPlanNote } from '../../shared/ui/data-plan-note';
import { HelpTip } from '../../shared/ui/help-tip';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { type SentimentSummary, safeUrl, sentimentWord, summarizeSentiment } from './calendar-view';

/**
 * The newest articles on the scope's tickers, and each ticker's sentiment
 * over the last 30 days. `query` null means the scope cannot have news
 * (everything, or no tickers named yet).
 */
@Component({
  selector: 'app-news-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, LoadingState, ErrorState, EmptyState, DataPlanNote, HelpTip],
  template: `
    <section class="page-section" aria-labelledby="news-title">
      <header class="section-head">
        <h2 id="news-title">News and sentiment <app-help-tip term="sentiment_score" /></h2>
      </header>
      <app-data-plan-note class="plan" kind="news" />
      @if (!query()) {
        <app-empty-state
          title="Pick whose news to show"
          message="News follows your holdings, a watchlist or the tickers you name. Everything is too much to read."
        />
      } @else if (news.error(); as err) {
        <app-error-state title="Could not load the news" [error]="err" (retry)="news.reload()" />
      } @else if (!news.hasValue()) {
        <app-loading-state label="Loading news" [rows]="5" />
      } @else if (news.value().items.length === 0 && news.value().sentiment.length === 0) {
        <app-empty-state
          title="No news yet"
          message="Articles and sentiment arrive with each data update for these tickers. Not every ticker has news every day."
        />
      } @else {
        <div class="body">
          @if (summary().length) {
            <div class="mood" role="list" aria-label="Sentiment by ticker, last 30 days">
              @for (s of summary(); track s.ticker) {
                <div class="mood-card" role="listitem">
                  <a class="mood-ticker" [routerLink]="['/charts', s.ticker]">{{ s.ticker }}</a>
                  <p class="mood-word" [class]="tone(s.average)">
                    <span class="shape" aria-hidden="true">{{ shape(s.average) }}</span>
                    {{ word(s.average) }}
                    <span class="num">{{ score(s.average) }}</span>
                  </p>
                  <p class="mood-meta">{{ meta(s) }}</p>
                </div>
              }
            </div>
          }
          @if (news.value().items.length) {
            <ul class="articles" aria-label="Newest articles">
              @for (a of news.value().items; track a.ticker + a.published_at + a.title) {
                <li class="article">
                  <span class="when num" [title]="date(a.published_at)">{{
                    ago(a.published_at)
                  }}</span>
                  <div class="article-body">
                    @if (link(a); as href) {
                      <a class="title" [href]="href" target="_blank" rel="noopener noreferrer"
                        >{{ a.title }}<span class="visually-hidden"> (opens in a new tab)</span></a
                      >
                    } @else {
                      <p class="title">{{ a.title }}</p>
                    }
                    <p class="article-head">
                      <span class="ticker-tag">{{ a.ticker }}</span>
                      @if (a.source_name) {
                        <span class="source">{{ a.source_name }}</span>
                      }
                      @if (a.sentiment !== null && a.sentiment !== undefined) {
                        <span class="tag" [class]="tone(a.sentiment)">
                          <span aria-hidden="true">{{ shape(a.sentiment) }}</span>
                          {{ word(a.sentiment) }}
                        </span>
                      }
                    </p>
                  </div>
                </li>
              }
            </ul>
          }
        </div>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    .plan {
      display: block;
      margin-top: var(--space-4);
    }
    .body {
      display: grid;
      gap: var(--space-6);
    }
    /* Each ticker's mood: a cell in a strip of hairline-ruled cells. */
    .mood {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(11rem, 1fr));
      border-bottom: 1px solid var(--rule);
      overflow: hidden;
    }
    .mood-card {
      display: grid;
      gap: 2px;
      min-width: 0;
      margin-left: -1px;
      padding: var(--space-3) var(--space-4);
      border-left: 1px solid var(--rule);
    }
    .mood-ticker {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      color: var(--ink);
      font-weight: 650;
      text-decoration: none;
      @include bp.from-tablet {
        min-height: 24px;
      }
    }
    .mood-ticker:hover {
      text-decoration: underline;
    }
    .mood-word {
      display: flex;
      align-items: baseline;
      gap: var(--space-1);
      font-size: var(--text-lead);
      font-weight: var(--weight-semibold);
    }
    .mood-meta {
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .articles {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    /* One article: the age in mono on the left, the title, then a meta line. */
    .article {
      display: grid;
      grid-template-columns: 5.5rem minmax(0, 1fr);
      gap: var(--space-1) var(--space-4);
      min-width: 0;
      padding: var(--space-3) 0;
      border-bottom: 1px solid var(--rule);
    }
    .article:last-child {
      border-bottom: 0;
    }
    .when {
      padding-top: 2px;
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .article-body {
      display: grid;
      gap: var(--space-1);
      min-width: 0;
    }
    .article-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-3);
      color: var(--muted);
      font-size: var(--text-xs);
    }
    .source {
      color: var(--ink-2);
    }
    .tag {
      display: inline-flex;
      gap: var(--space-1);
      font-weight: var(--weight-label);
    }
    .title {
      max-width: 72ch;
      color: var(--ink);
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
      line-height: 1.4;
      overflow-wrap: anywhere;
      text-decoration: none;
    }
    a.title:hover,
    a.title:focus-visible {
      text-decoration: underline;
    }
    @include bp.phone {
      .article {
        grid-template-columns: minmax(0, 1fr);
      }
      .when {
        order: 2;
        padding: 0;
      }
      a.title {
        display: inline-block;
        min-height: var(--touch-min);
        padding: var(--space-1) 0;
      }
    }
  `,
})
export class NewsPanel {
  private readonly api = inject(CalendarsService);

  readonly query = input<NewsQuery | null>(null);

  protected readonly news = resource({
    params: () => this.query() ?? undefined,
    loader: ({ params }) => this.api.news(params),
  });

  protected readonly summary = computed(() =>
    this.news.hasValue() ? summarizeSentiment(this.news.value().sentiment) : [],
  );

  protected readonly word = sentimentWord;
  protected readonly ago = (v: string) => formatAgo(v);
  protected readonly date = (v: string) => formatDate(v);
  protected link(a: NewsItem): string | null {
    return safeUrl(a.url);
  }

  protected score(v: number | null): string {
    return v === null ? '' : formatNumber(v, { digits: 2, signed: true });
  }

  protected tone(v: number | null | undefined): string {
    const w = sentimentWord(v);
    return w === 'Positive' ? 'gain' : w === 'Negative' ? 'loss' : '';
  }

  /** A shape next to the word, so the mood is not colour alone. */
  protected shape(v: number | null | undefined): string {
    const w = sentimentWord(v);
    return w === 'Positive' ? '▲' : w === 'Negative' ? '▼' : '●';
  }

  protected meta(s: SentimentSummary): string {
    const articles = s.articles === 1 ? '1 article' : `${formatNumber(s.articles)} articles`;
    const days = s.days === 1 ? '1 day' : `${s.days} days`;
    return `${articles} over ${days}`;
  }
}
