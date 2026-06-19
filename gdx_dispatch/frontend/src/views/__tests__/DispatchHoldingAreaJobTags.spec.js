/**
 * Dispatch — Holding-area job card tags (GDX-correct contract).
 *
 * Doug 2026-05-17: "if it is in a container that says Order Doors why should
 * it have a tag that says scheduled? ... it should have both tags." Then,
 * before commit: "are you using the right coding for gdx? ... look at that
 * first, make sure it is correct."
 *
 * Verified against GDX prod (gdx-postgres:gdx_xdg): 8 real holding areas
 * (Order Doors #3b82f6 blue, Needs Parts #f59e0b amber, 4 default-gray,
 * Ready to Schedule green, Waiting on Customer purple). Job-status reality:
 * Scheduled / Service Call / Complete / Completed — and 77% of jobs have an
 * EMPTY status. That data drove three corrections to the first cut:
 *
 *  - The status tag does NOT get statusSeverity() coloring (that was an
 *    unprompted product decision; GDX's vocabulary doesn't map to those
 *    keywords — Scheduled and Service Call both fall through to generic
 *    blue). It stays a plain chip, exactly as it was pre-change.
 *  - The status tag is suppressed when status is blank, so the 77% of jobs
 *    with empty status don't get a meaningless empty colored pill.
 *  - The area tag's text color is luminance-derived (readableText), not a
 *    hardcoded white — white on GDX's amber "Needs Parts" is unreadable.
 *
 * Mirrors DispatchHoldingAreaCreate.spec.js: a mini host reproduces the
 * real view's holding-area card fragment (DispatchView is too heavy for
 * unit tests). readableText() is kept in lockstep with DispatchView so the
 * contrast assertions exercise the real mapping.
 *
 * Contract pinned:
 *  1. A parked job WITH a status renders both an area-name tag and a status
 *     tag; the status tag is plain (no severity).
 *  2. A parked job with EMPTY status renders only the area tag.
 *  3. The area tag is tinted to the area color with readable text:
 *     white on dark fills (Order Doors blue), dark ink on light fills
 *     (Needs Parts amber).
 */
import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { defineComponent, ref } from 'vue';

const Tag = {
  name: 'Tag',
  props: ['value', 'severity', 'size'],
  inheritAttrs: false,
  template:
    '<span class="tag-stub" :data-value="value" :data-severity="severity || \'\'"' +
    ' :class="$attrs.class" :style="$attrs.style">{{ value }}</span>',
};

// Lockstep mirror of DispatchView.vue readableText().
function readableText(hex) {
  const c = String(hex || '').replace('#', '');
  if (c.length !== 6) return '#ffffff';
  const r = parseInt(c.slice(0, 2), 16);
  const g = parseInt(c.slice(2, 4), 16);
  const b = parseInt(c.slice(4, 6), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.6 ? '#1f2937' : '#ffffff';
}

const Host = defineComponent({
  components: { Tag },
  setup() {
    // Real GDX area colors.
    const areas = ref([
      { id: 'a1', name: 'Order Doors', color: '#3b82f6' }, // blue → white text
      { id: 'a2', name: 'Needs Parts', color: '#f59e0b' }, // amber → dark text
    ]);
    const jobs = ref([
      { id: 'j1', customer: 'Acme Co', status: 'Scheduled', holding_area_id: 'a1' },
      { id: 'j2', customer: 'Beta LLC', status: '', holding_area_id: 'a1' }, // 77% case
      { id: 'j3', customer: 'Gamma In', status: 'Complete', holding_area_id: 'a2' },
    ]);
    const getHoldingAreaJobs = (areaId) =>
      jobs.value.filter((j) => j.holding_area_id === areaId);
    return { areas, getHoldingAreaJobs, readableText };
  },
  template: `
    <div>
      <div v-for="area in areas" :key="area.id">
        <div
          v-for="job in getHoldingAreaJobs(area.id)"
          :key="job.id"
          class="job-card holding-job-card"
          :data-testid="'holding-job-' + job.id"
        >
          <div class="job-customer-name">{{ job.customer }}</div>
          <Tag :value="area.name" size="small" class="holding-area-tag"
            :style="{ backgroundColor: area.color, color: readableText(area.color), borderColor: area.color }" />
          <Tag v-if="job.status" :value="job.status" size="small" />
        </div>
      </div>
    </div>
  `,
});

describe('Dispatch — holding-area job card tags (GDX-correct)', () => {
  it('renders BOTH an area tag and a plain status tag when status is present', () => {
    const wrapper = mount(Host);
    const tags = wrapper.find('[data-testid="holding-job-j1"]').findAll('.tag-stub');

    expect(tags).toHaveLength(2);
    const values = tags.map((t) => t.attributes('data-value'));
    expect(values).toContain('Order Doors'); // the container it sits in
    expect(values).toContain('Scheduled'); // its lifecycle status

    // Status tag must be plain — no statusSeverity() coloring (that was the
    // unprompted decision; GDX's vocabulary doesn't map to its keywords).
    const statusTag = tags.find((t) => t.attributes('data-value') === 'Scheduled');
    expect(statusTag.attributes('data-severity')).toBe('');
  });

  it('suppresses the status tag for the 77% of jobs with empty status', () => {
    const wrapper = mount(Host);
    const tags = wrapper.find('[data-testid="holding-job-j2"]').findAll('.tag-stub');

    // j2 is in Order Doors; only the area tag should render, no empty pill.
    expect(tags).toHaveLength(1);
    expect(tags[0].attributes('data-value')).toBe('Order Doors');
  });

  it('tints the area tag to the area color with readable text per luminance', () => {
    const wrapper = mount(Host);

    const blueAreaTag = wrapper
      .find('[data-testid="holding-job-j1"]')
      .findAll('.tag-stub')
      .find((t) => t.attributes('data-value') === 'Order Doors');
    const blueStyle = blueAreaTag.attributes('style') || '';
    expect(blueStyle).toContain('background-color: rgb(59, 130, 246)'); // #3b82f6
    expect(blueStyle).toContain('color: rgb(255, 255, 255)'); // dark fill → white ink

    const amberAreaTag = wrapper
      .find('[data-testid="holding-job-j3"]')
      .findAll('.tag-stub')
      .find((t) => t.attributes('data-value') === 'Needs Parts');
    const amberStyle = amberAreaTag.attributes('style') || '';
    expect(amberStyle).toContain('background-color: rgb(245, 158, 11)'); // #f59e0b
    expect(amberStyle).toContain('color: rgb(31, 41, 55)'); // light fill → dark ink
  });
});
