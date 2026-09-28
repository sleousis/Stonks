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
  imports: [RouterLink, LoadingState, ErrorState, EmptyState],
  template: `
    <section class="panel" aria-labelledby="news-title">
      <div class="panel-head">
        <h2 id="news-title">News and sentiment</h2>
      </div>
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
          message="Articles and sentiment arrive with each data update for these tickers."
        />
      } @else {
        <div class="panel-body body">
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
                  <p class="article-head">
                    <span class="ticker">{{ a.ticker }}</span>
                    <span class="when" [title]="date(a.published_at)">{{
                      ago(a.published_at)
                    }}</span>
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
                  @if (link(a); as href) {
                    <a class="title" [href]="href" target="_blank" rel="noopener noreferrer"
                      >{{ a.title }}<span class="visually-hidden"> (opens in a new tab)</span></a
                    >
                  } @else {
                    <p class="title">{{ a.title }}</p>
                  }
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
    .body {
      display: grid;
      gap: var(--space-4);
    }
    .mood {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(10rem, 1fr));
      gap: var(--space-2);
    }
    .mood-card {
      display: grid;
      gap: var(--space-1);
      padding: var(--space-3);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      background: var(--color-surface-2);
    }
    .mood-ticker {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      font-family: var(--font-mono);
      font-weight: var(--weight-semibold);
      @include bp.from-tablet {
        min-height: 24px;
      }
    }
    .mood-word {
      display: flex;
      align-items: baseline;
      gap: var(--space-1);
      font-weight: var(--weight-medium);
    }
    .mood-meta {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .articles {
      display: grid;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .article {
      display: grid;
      gap: var(--space-1);
      padding: var(--space-3) 0;
      border-bottom: 1px solid var(--color-border);
      min-width: 0;
    }
    .article:last-child {
      border-bottom: 0;
    }
    .article-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-3);
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .ticker {
      font-family: var(--font-mono);
      font-weight: var(--weight-semibold);
      color: var(--color-ink);
    }
    .tag {
      display: inline-flex;
      gap: var(--space-1);
      padding: 0 var(--space-2);
      border: 1px solid currentColor;
      border-radius: var(--radius-xs);
    }
    .title {
      display: inline-block;
      min-height: var(--touch-min);
      padding: var(--space-1) 0;
      overflow-wrap: anywhere;
      @include bp.from-tablet {
        min-height: 24px;
      }
    }
    .gain {
      color: var(--color-gain);
    }
    .loss {
      color: var(--color-loss);
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
