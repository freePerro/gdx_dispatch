/**
 * Zero-price warning surfaces (PR1-billing-capture 2026-07-07).
 *
 * The audit found the F-75 warn-mode strings were emitted by the API but
 * rendered by NOTHING — a warning no human sees is theater. Pinned here:
 *
 *  1. InvoiceCreateView renders `warnings` from POST /api/invoices as a
 *     warn toast.
 *  2. InvoiceDetailView renders the per-line `warning` from
 *     POST /api/invoices/{id}/lines as a warn toast.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const CREATE_SRC = readFileSync(
  join(__dirname, '..', 'InvoiceCreateView.vue'),
  'utf8',
);
const DETAIL_SRC = readFileSync(
  join(__dirname, '..', 'InvoiceDetailView.vue'),
  'utf8',
);

describe('InvoiceCreateView — zero-price warnings toast', () => {
  it('surfaces created.warnings as a warn toast', () => {
    const postIdx = CREATE_SRC.indexOf("await api.post('/api/invoices', payload)");
    expect(postIdx).toBeGreaterThan(-1);
    // Window widened 700→1600 (2026-07-23): the double-billing 409
    // confirm-retry and deposit-netting toast now sit between the POST and
    // the warnings toast. The pin is "warnings are rendered", not distance.
    const span = CREATE_SRC.slice(postIdx, postIdx + 1600);
    expect(span).toMatch(/created\.warnings/);
    expect(span).toMatch(/severity:\s*'warn'/);
    expect(span).toMatch(/created\.warnings\.join/);
  });
});

describe('InvoiceDetailView — per-line zero-price warning toast', () => {
  it('surfaces the add-line warning as a warn toast', () => {
    const postIdx = DETAIL_SRC.indexOf('await api.post(`/api/invoices/${id}/lines`, body)');
    expect(postIdx).toBeGreaterThan(-1);
    const span = DETAIL_SRC.slice(postIdx - 50, postIdx + 700);
    expect(span).toMatch(/lineResp\.warning/);
    expect(span).toMatch(/severity:\s*'warn'/);
  });
});
