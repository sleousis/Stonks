import type { OnboardingStepView, OnboardingView, SystemCheckView } from '../../api/models';
import type { OnboardingStepId } from '../../api/onboarding.service';

/** What each first-run step asks, in trader words. */
export const STEP_COPY: Record<OnboardingStepId, { title: string; lead: string; done: string }> = {
  account: {
    title: 'Protect your account',
    lead: 'Your password and a code from your authenticator app keep your money safe.',
    done: 'Your authenticator is set up.',
  },
  portfolio: {
    title: 'Pick a portfolio',
    lead: 'A portfolio holds the trades a strategy makes for you. Start with a paper one.',
    done: 'You have a portfolio.',
  },
  data: {
    title: 'Choose what to watch',
    lead: 'Save the tickers you care about as a watchlist. Today and the charts can show only them.',
    done: 'You have a watchlist.',
  },
  follow: {
    title: 'Follow a strategy',
    lead: 'Get its signals, or let it trade your paper portfolio.',
    done: 'You follow a strategy.',
  },
  alerts: {
    title: 'Turn on alerts',
    lead: 'Signals and fills reach this device, even when the console is closed.',
    done: 'This account gets alerts on a device.',
  },
};

/** The admin's install checks: what each means and where to fix it. */
export const SYSTEM_COPY: Record<
  SystemCheckView['id'],
  { title: string; fix: string; link: string }
> = {
  data_source: {
    title: 'Data source key',
    fix: 'Add the market data key on the server, then check the sources here.',
    link: '/data',
  },
  first_ingest: { title: 'First data load', fix: 'Load prices for a few tickers.', link: '/data' },
  strategies: {
    title: 'Strategies to follow',
    fix: 'Install the starter set: three simple strategies go on trial.',
    link: '/strategies',
  },
  backup: { title: 'Backups', fix: 'Make the first backup.', link: '/ops/schedule' },
  scheduler: {
    title: 'Scheduler',
    fix: 'Start the scheduler so the daily runs happen on their own.',
    link: '/ops/schedule',
  },
};

/** The step to open first: the first still to do, else none. */
export function currentStep(view: OnboardingView): OnboardingStepId | null {
  return view.steps.find((s) => s.state === 'todo')?.id ?? null;
}

/** "3 of 5 done" (skipped steps count as handled). */
export function progressText(steps: readonly OnboardingStepView[]): string {
  const done = steps.filter((s) => s.state === 'done').length;
  const skipped = steps.filter((s) => s.state === 'skipped').length;
  const base = `${done} of ${steps.length} done`;
  return skipped ? `${base}, ${skipped} skipped` : base;
}

/** Split typed tickers on commas, spaces or new lines, upper-cased, once each. */
export function parseTickerText(text: string): string[] {
  const seen = new Set<string>();
  for (const raw of text.split(/[\s,;]+/)) {
    const t = raw.trim().toUpperCase();
    if (t) seen.add(t);
  }
  return [...seen];
}
