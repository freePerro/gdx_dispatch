// The /pay page is a server-rendered template, not a Vue view, so its
// Stripe.js error filter is lifted out of the template and run here.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

const html = readFileSync(resolve(__dirname, '../../../../templates/payment_form.html'), 'utf8');
const start = html.indexOf('  const PAYMENT_UNAVAILABLE =');
const end = html.indexOf('return PAYMENT_UNAVAILABLE;\n  }', start) + 'return PAYMENT_UNAVAILABLE;\n  }'.length;
// eslint-disable-next-line no-new-func
const stripeErrorText = new Function(`${html.slice(start, end)}\nreturn stripeErrorText;`)();

describe('/pay Stripe.js error filter', () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});

  it('shows the payer what is about their own card or what they typed', () => {
    expect(stripeErrorText({ type: 'card_error', message: 'Your card was declined.' })).toBe('Your card was declined.');
    expect(stripeErrorText({ type: 'validation_error', message: 'Your card number is incomplete.' }))
      .toBe('Your card number is incomplete.');
  });

  it('sends a failed 3D Secure check to another card, not back to the same one later', () => {
    for (const code of ['payment_intent_authentication_failure', 'setup_intent_authentication_failure']) {
      const text = stripeErrorText({
        type: 'invalid_request_error', code,
        message: 'We are unable to authenticate your payment method.',
      });
      expect(text).toContain('different card');
      expect(text).not.toContain('try again in a few minutes');
    }
  });

  it('never shows text that names our setup', () => {
    for (const error of [
      { type: 'invalid_request_error', message: 'Invalid API Key provided: pk_test_abc' },
      { type: 'api_error', message: 'Something went wrong on our end' },
      { type: 'authentication_error', message: 'No valid API key provided' },
      undefined,
    ]) {
      const text = stripeErrorText(error);
      expect(text).toContain("We couldn't process this payment right now");
      expect(text).not.toContain('API');
    }
  });
});
