import {
  ACCOUNT_RULE_WORDS,
  SAFEGUARD_WORDS,
  accountRuleWords,
  liveAdjustmentLabel,
  profileNotes,
  safeguardWords,
} from './live-rules';

describe('live rule words', () => {
  it('names every live safeguard and account rule the server has', () => {
    expect(Object.keys(SAFEGUARD_WORDS)).toEqual([
      'capital_ramp',
      'live_notional_caps',
      'price_band',
      'account_rules',
      'max_orders_per_run',
      'stop_cooldown',
      'stop_guard',
      'losing_lock',
    ]);
    for (const name of [
      'restricted',
      'short_permission',
      'account_known',
      'settled_cash',
      'buying_power',
      'fx_funding',
      'pdt',
      'wash_sale',
      'reg_sho',
      'priips_kid',
      'short_disclosure',
    ]) {
      expect(ACCOUNT_RULE_WORDS[name]?.effect).toBeTruthy();
    }
  });

  it('keeps an unknown rule readable', () => {
    expect(safeguardWords('new_rule')).toEqual({ label: 'New rule', effect: '' });
    expect(accountRuleWords('odd_lot').label).toBe('Odd lot');
  });

  it('labels a trading run adjustment from a live rule', () => {
    expect(liveAdjustmentLabel('account_rules.settled_cash')).toBe(
      'Account rule: Settled cash only',
    );
    expect(liveAdjustmentLabel('account_rules.pdt')).toBe('Account rule: Pattern day trader');
    expect(liveAdjustmentLabel('price_band')).toBe('Price band');
    expect(liveAdjustmentLabel('max_weight_per_ticker')).toBeNull();
  });

  it('explains what a profile means', () => {
    const usCash = profileNotes({
      jurisdiction: 'us',
      account_type: 'cash',
      client_class: 'retail',
    });
    expect(usCash[0]).toContain('settled cash only');
    expect(usCash[0]).toContain('the next trading day');
    expect(usCash.join(' ')).not.toContain('day trades');

    const usMargin = profileNotes({
      jurisdiction: 'us',
      account_type: 'margin',
      client_class: 'retail',
    });
    expect(usMargin.join(' ')).toContain('day trades');

    const ukRetail = profileNotes({
      jurisdiction: 'uk',
      account_type: 'cash',
      client_class: 'retail',
    });
    expect(ukRetail[0]).toContain('two trading days later');
    expect(ukRetail.join(' ')).toContain('key information document');

    const euPro = profileNotes({
      jurisdiction: 'eu',
      account_type: 'margin',
      client_class: 'professional',
    });
    expect(euPro.join(' ')).toContain('does not apply');
  });
});
