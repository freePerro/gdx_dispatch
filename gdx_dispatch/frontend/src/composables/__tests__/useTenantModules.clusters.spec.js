/**
 * collapseClusters — 2026-07-07 tabbed-pages.
 *
 * Within a category's (already enablement+permission-filtered) module list,
 * a run of modules sharing a `cluster` key collapses to ONE hub row. Pins:
 *   - hub replaces the children at the FIRST child's position
 *   - hub targets the first visible child (`to`) so a user missing one
 *     tab's permission still lands on a page they can read
 *   - hub carries `matchPaths` for sidebar active-state matching
 *   - non-clustered modules pass through untouched, order preserved
 *   - a cluster whose children were all filtered out emits nothing
 *     (callers drop empty categories; the hub must not resurrect them)
 */
import { describe, it, expect } from 'vitest';
import { collapseClusters } from '../useTenantModules';
import { NAV_CLUSTERS } from '../../constants/modules';

const mk = (key, cluster, to = `/${key}`) => ({
  key,
  label: key,
  icon: 'pi pi-box',
  to,
  ...(cluster ? { cluster } : {}),
});

describe('collapseClusters', () => {
  it('collapses a cluster run into one hub row at the first child position', () => {
    const modules = [
      mk('customers'),
      mk('reviews', 'reputation_hub'),
      mk('referrals', 'reputation_hub'),
      mk('surveys', 'reputation_hub'),
      mk('booking'),
    ];
    const out = collapseClusters(modules);
    expect(out.map((m) => m.key)).toEqual(['customers', 'reputation_hub', 'booking']);
    const hub = out[1];
    expect(hub.label).toBe('Reputation');
    expect(hub.to).toBe('/reviews');
    expect(hub.matchPaths).toEqual(['/reviews', '/referrals', '/surveys']);
  });

  it('targets the first VISIBLE child when the lead child is filtered out', () => {
    // e.g. user lacks invoices.read_all but holds payments.read: the
    // billing child never reaches collapseClusters, payments leads.
    const modules = [
      mk('payments', 'billing_hub', '/payments'),
      mk('collections', 'billing_hub', '/collections'),
    ];
    const [hub] = collapseClusters(modules);
    expect(hub.to).toBe('/payments');
    expect(hub.matchPaths).toEqual(['/payments', '/collections']);
  });

  it('emits nothing for a cluster with no surviving children', () => {
    const out = collapseClusters([mk('jobs'), mk('timeclock')]);
    expect(out.map((m) => m.key)).toEqual(['jobs', 'timeclock']);
    expect(out.some((m) => m.clusterHub)).toBe(false);
  });

  it('uses the NAV_CLUSTERS label/icon for every defined cluster', () => {
    for (const cluster of NAV_CLUSTERS) {
      const [hub] = collapseClusters([mk('child-a', cluster.key), mk('child-b', cluster.key)]);
      expect(hub.key).toBe(cluster.key);
      expect(hub.label).toBe(cluster.label);
      expect(hub.icon).toBe(cluster.icon);
      expect(hub.clusterHub).toBe(cluster.key);
    }
  });

  it('collapses two different clusters in one category independently', () => {
    const modules = [
      mk('fleet', 'fleet_hub'),
      mk('gps', 'fleet_hub'),
      mk('daily_loadsheet', 'loadsheets_hub'),
      mk('delivery_loadsheet', 'loadsheets_hub'),
    ];
    const out = collapseClusters(modules);
    expect(out.map((m) => m.key)).toEqual(['fleet_hub', 'loadsheets_hub']);
  });
});
