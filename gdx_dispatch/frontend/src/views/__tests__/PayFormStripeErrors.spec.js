// The /pay page is a server-rendered template, not a Vue view, so its
// Stripe.js error filter is lifted out of the template and run here. The
// payer-fixable list is a Jinja global from core/payments.py; test_payments.py
// proves the rendered page carries exactly that dict, so here it is a stand-in
// with the same keys.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

const PAYER_FIXABLE = {
  payment_intent_authentication_failure: "Your bank couldn't verify this card. Please use a different card or payment method.",
  setup_intent_authentication_failure: "Your bank couldn't verify this card. Please use a different card or payment method.",
  email_invalid: "That email address doesn't look right. Please check it and try again.",
  routing_number_invalid: "That routing number isn't valid. Please check it and try again.",
  account_number_invalid: "That account number isn't valid. Please check it and try again.",
  bank_account_unusable: "This bank account can't be used. Please use a different bank account or pay by card.",
  payment_method_customer_decline: "The payment wasn't approved. Please try again or use a different payment method.",
};
const JINJA = '{{ payer_fixable_stripe_details | tojson }}';

const html = readFileSync(resolve(__dirname, '../../../../templates/payment_form.html'), 'utf8');
const start = html.indexOf('  const PAYMENT_UNAVAILABLE =');
const tail = 'const EMAIL_SHAPE = /';
const end = html.indexOf(';\n', html.indexOf(tail, start)) + 1;
const block = html.slice(start, end);
// eslint-disable-next-line no-new-func
const { stripeErrorText, EMAIL_SHAPE } = new Function(
  `${block.replace(JINJA, JSON.stringify(PAYER_FIXABLE))}\nreturn { stripeErrorText, EMAIL_SHAPE };`,
)();

describe('/pay Stripe.js error filter', () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});

  it('reads the payer-fixable list from the server, not a copy', () => {
    expect(block).toContain(`const PAYER_FIXABLE = ${JINJA};`);
  });

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

  it('shows a payer-fixable bank or email error on a non-card type with our text, not Stripe\'s', () => {
    for (const code of Object.keys(PAYER_FIXABLE)) {
      for (const type of ['invalid_request_error', 'api_error', undefined]) {
        const text = stripeErrorText({ type, code, message: 'Invalid email: a@b (req_123, billing_details[email])' });
        expect(text).toBe(PAYER_FIXABLE[code]);
        expect(text).not.toContain('req_');
      }
    }
  });

  it('never shows text that names our setup', () => {
    for (const error of [
      { type: 'invalid_request_error', message: 'Invalid API Key provided: pk_test_abc' },
      { type: 'invalid_request_error', code: 'resource_missing', message: 'No such payment_intent: pi_123' },
      { type: 'invalid_request_error', code: 'toString', message: 'prototype key, not a listed code' },
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

describe('/pay ACH email check', () => {
  it('refuses what type="email" lets through but Stripe calls email_invalid', () => {
    for (const email of ['a@b', 'a@b.', 'a@.com', 'a b@c.com', '@c.com', 'a@b..com', 'a@@b.com']) {
      expect(EMAIL_SHAPE.test(email), email).toBe(false);
    }
  });

  it('takes an ordinary address', () => {
    for (const email of ['you@example.com', 'first.last+tag@mail.co.uk', 'a@b.io']) {
      expect(EMAIL_SHAPE.test(email), email).toBe(true);
    }
  });
});
