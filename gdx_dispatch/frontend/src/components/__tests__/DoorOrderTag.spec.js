import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import DoorOrderTag from '../DoorOrderTag.vue';

const render = (doors) => mount(DoorOrderTag, { props: { doors }, global: { plugins: [PrimeVue] } });

describe('DoorOrderTag', () => {
  it('renders nothing when the job has no captured door', () => {
    expect(render(null).find('[data-testid="door-order-tag"]').exists()).toBe(false);
    expect(render({ total: 0, ordered: 0 }).find('[data-testid="door-order-tag"]').exists()).toBe(false);
  });

  it('says the doors are ordered once every door is', () => {
    expect(render({ total: 2, ordered: 2 }).text()).toContain('Doors ordered');
    expect(render({ total: 1, ordered: 1 }).text()).toContain('Door ordered');
  });

  it('says what is still to order', () => {
    expect(render({ total: 2, ordered: 0 }).text()).toContain('Doors to order');
    expect(render({ total: 3, ordered: 1 }).text()).toContain('1 of 3 doors ordered');
  });
});
