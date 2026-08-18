/**
 * doorSizeLabel — the auto-label a captured door photo gets (Doug's format:
 * 16' × 7'). The spec shape belongs to the plugin (ADR-013), so the helper
 * probes common keys and falls back to the description; when nothing is
 * known it returns blank — a wrong size on a customer's photo is worse than
 * no label.
 */
import { describe, expect, it } from 'vitest';

import { doorSizeLabel } from './doorSizeLabel';

describe('doorSizeLabel', () => {
  it('reads width/height from a nested spec (feet)', () => {
    expect(doorSizeLabel({ line_metadata: { spec: { width: 16, height: 7 } } })).toBe("16' × 7'");
    expect(doorSizeLabel({ line_metadata: { door_spec: { width_ft: 9, height_ft: 8 } } })).toBe("9' × 8'");
  });

  it('converts inch-valued fields (the *_ft-holding-inches trap)', () => {
    // The labor matrix stores inches in width_ft/height_ft; a captured spec
    // may do the same. 192in × 84in is a 16' × 7' door, not a 192' one.
    expect(doorSizeLabel({ line_metadata: { width_ft: 192, height_ft: 84 } })).toBe("16' × 7'");
  });

  it('falls back to the WxH in the description', () => {
    expect(doorSizeLabel({ description: 'CHI 2251 16x7 insulated' })).toBe("16' × 7'");
    expect(doorSizeLabel({ description: 'CHI 2251, 9 X 8, white' })).toBe("9' × 8'");
    expect(doorSizeLabel({ description: '16 × 7 stamped carriage' })).toBe("16' × 7'");
  });

  it('never mislabels from numbers that are not door dimensions', () => {
    expect(doorSizeLabel({ description: 'part 216x750 bracket' })).toBe('');
    expect(doorSizeLabel({ description: 'torsion spring 32x62' })).toBe('');
    // Lumber is not a door (fenced by the 4–24' × 6–24' plausibility window).
    expect(doorSizeLabel({ description: 'Frame opening with 2x4s' })).toBe('');
    // Pixel dimensions in plugin metadata must not become "67' × 50'".
    expect(doorSizeLabel({ line_metadata: { spec: { width: 800, height: 600 } } })).toBe('');
  });

  it('returns blank when nothing is known — never guesses', () => {
    expect(doorSizeLabel({ description: 'Spring replacement' })).toBe('');
    expect(doorSizeLabel(null)).toBe('');
    expect(doorSizeLabel({ line_metadata: { spec: { width: 0, height: 7 } } })).toBe('');
    // A 7'6" special (90in) is not whole feet — hand-label rather than round to 8'.
    expect(doorSizeLabel({ line_metadata: { spec: { width_in: 192, height_in: 90 } } })).toBe('');
  });
});
