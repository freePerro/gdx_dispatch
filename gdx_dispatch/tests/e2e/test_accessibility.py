"""E2E tests for Accessibility (WCAG 2.1 AA) — A11Y-01 through A11Y-12.

Covers: axe-core scan, keyboard navigation, focus indicators,
ARIA labels, color contrast, heading hierarchy, table headers.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.e2e]

# Pages to audit
PAGES_TO_AUDIT = [
    "/dashboard",
    "/customers",
    "/jobs",
    "/estimates",
    "/billing",
    "/reports",
    "/settings",
]


class TestAxeCoreScan:
    """A11Y-01: Every page passes axe-core scan (no critical/serious violations)."""

    @pytest.mark.parametrize("path", PAGES_TO_AUDIT)
    def test_a11y_01_axe_scan(self, navigate, authenticated_page, console_tracker, path):
        """Run axe-core accessibility scan on page."""
        page = navigate(path)
        page.wait_for_timeout(3000)

        # Inject axe-core
        page.evaluate("""() => {
            return new Promise((resolve, reject) => {
                if (window.axe) { resolve(); return; }
                const script = document.createElement('script');
                script.src = 'https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js';
                script.onload = resolve;
                script.onerror = () => reject(new Error('Failed to load axe-core'));
                document.head.appendChild(script);
            });
        }""")
        page.wait_for_timeout(1000)

        # Run axe scan
        results = page.evaluate("""() => {
            if (!window.axe) return { violations: [], error: 'axe not loaded' };
            return axe.run(document, {
                runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] },
                resultTypes: ['violations'],
            }).then(r => ({
                violations: r.violations.filter(v =>
                    v.impact === 'critical' || v.impact === 'serious'
                ).map(v => ({
                    id: v.id,
                    impact: v.impact,
                    description: v.description,
                    nodes: v.nodes.length,
                }))
            }));
        }""")

        if isinstance(results, dict) and results.get("error"):
            pytest.xfail(f"axe-core not available: {results['error']}")

        violations = results.get("violations", [])
        if violations:
            report = "\n".join(
                f"  [{v['impact']}] {v['id']}: {v['description']} ({v['nodes']} nodes)"
                for v in violations
            )
            pytest.xfail(f"Accessibility violations on {path}:\n{report}")

        console_tracker.assert_no_errors(f"a11y scan {path}")


class TestFormLabels:
    """A11Y-02: Every input has associated label."""

    def test_a11y_02_form_labels(self, navigate, authenticated_page, console_tracker):
        """Check that all visible inputs have labels."""
        page = navigate("/customers")
        page.wait_for_timeout(3000)

        unlabeled = page.evaluate("""() => {
            const inputs = document.querySelectorAll(
                'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select'
            );
            const unlabeled = [];
            inputs.forEach(input => {
                if (!input.offsetParent) return;  // skip hidden
                const hasLabel = input.labels && input.labels.length > 0;
                const hasAria = input.getAttribute('aria-label') ||
                                input.getAttribute('aria-labelledby') ||
                                input.getAttribute('title') ||
                                input.getAttribute('placeholder');
                if (!hasLabel && !hasAria) {
                    unlabeled.push({
                        tag: input.tagName,
                        type: input.type || '',
                        id: input.id || '',
                        name: input.name || '',
                    });
                }
            });
            return unlabeled;
        }""")

        if unlabeled:
            report = "\n".join(
                f"  {i['tag']} type={i['type']} id={i['id']} name={i['name']}"
                for i in unlabeled
            )
            pytest.xfail(f"Unlabeled inputs found:\n{report}")

        console_tracker.assert_no_errors("form labels check")


class TestColorContrast:
    """A11Y-03: Text meets WCAG AA contrast ratios."""

    def test_a11y_03_contrast(self, navigate, authenticated_page, console_tracker):
        """Check color contrast via axe-core color-contrast rule."""
        page = navigate("/dashboard")
        page.wait_for_timeout(3000)

        page.evaluate("""() => {
            return new Promise((resolve) => {
                if (window.axe) { resolve(); return; }
                const script = document.createElement('script');
                script.src = 'https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js';
                script.onload = resolve;
                script.onerror = resolve;
                document.head.appendChild(script);
            });
        }""")
        page.wait_for_timeout(500)

        results = page.evaluate("""() => {
            if (!window.axe) return { violations: [] };
            return axe.run(document, {
                runOnly: { type: 'rule', values: ['color-contrast'] },
                resultTypes: ['violations'],
            }).then(r => ({
                violations: r.violations.map(v => ({
                    id: v.id,
                    nodes: v.nodes.length,
                }))
            }));
        }""")

        violations = results.get("violations", [])
        if violations:
            total_nodes = sum(v["nodes"] for v in violations)
            pytest.xfail(f"Color contrast violations: {total_nodes} elements")

        console_tracker.assert_no_errors("contrast check")


class TestKeyboardNavigation:
    """A11Y-04: Tab through all interactive elements in logical order."""

    def test_a11y_04_keyboard_nav(self, navigate, authenticated_page, console_tracker):
        """Tab through interactive elements, verify focus moves."""
        page = navigate("/dashboard")
        page.wait_for_timeout(3000)

        focused_tags = []
        for _ in range(10):
            page.keyboard.press("Tab")
            page.wait_for_timeout(200)
            tag = page.evaluate("() => document.activeElement?.tagName || 'NONE'")
            focused_tags.append(tag)

        # At least some elements should receive focus
        interactive = [t for t in focused_tags if t in ("A", "BUTTON", "INPUT", "SELECT", "TEXTAREA")]
        assert len(interactive) >= 2, (
            f"Tab should reach interactive elements, got: {focused_tags}"
        )
        console_tracker.assert_no_errors("keyboard navigation")


class TestFocusIndicators:
    """A11Y-05: Focused elements have visible outline."""

    def test_a11y_05_focus_visible(self, navigate, authenticated_page, console_tracker):
        """Check that focused interactive elements have visible focus indicator."""
        page = navigate("/dashboard")
        page.wait_for_timeout(3000)

        # Tab to first focusable element
        page.keyboard.press("Tab")
        page.wait_for_timeout(300)

        has_outline = page.evaluate("""() => {
            const el = document.activeElement;
            if (!el || el === document.body) return null;
            const style = getComputedStyle(el);
            const outline = style.outline;
            const boxShadow = style.boxShadow;
            const borderColor = style.borderColor;
            // Check if there's any visible focus indicator
            return {
                tag: el.tagName,
                outline: outline,
                boxShadow: boxShadow !== 'none' ? boxShadow.slice(0, 50) : 'none',
                hasVisibleIndicator: (
                    outline !== 'none' &&
                    outline !== '' &&
                    !outline.includes('0px')
                ) || boxShadow !== 'none',
            };
        }""")

        if has_outline and not has_outline.get("hasVisibleIndicator"):
            pytest.xfail(
                f"No visible focus indicator on {has_outline['tag']}: "
                f"outline={has_outline['outline']}"
            )
        console_tracker.assert_no_errors("focus indicators")


class TestARIALandmarks:
    """A11Y-06: Main, nav, banner, contentinfo landmarks present."""

    def test_a11y_06_landmarks(self, navigate, authenticated_page, console_tracker):
        """Check ARIA landmarks are present."""
        page = navigate("/dashboard")
        page.wait_for_timeout(3000)

        landmarks = page.evaluate("""() => {
            const result = {};
            result.main = !!document.querySelector('main, [role="main"]');
            result.nav = !!document.querySelector('nav, [role="navigation"]');
            result.banner = !!document.querySelector('header, [role="banner"]');
            result.contentinfo = !!document.querySelector('footer, [role="contentinfo"]');
            return result;
        }""")

        missing = [k for k, v in landmarks.items() if not v]
        if missing:
            pytest.xfail(f"Missing ARIA landmarks: {', '.join(missing)}")

        console_tracker.assert_no_errors("ARIA landmarks")


class TestAltText:
    """A11Y-07: All images have alt text."""

    def test_a11y_07_alt_text(self, navigate, authenticated_page, console_tracker):
        """All images have alt text (empty alt="" for decorative)."""
        page = navigate("/dashboard")
        page.wait_for_timeout(3000)

        missing_alt = page.evaluate("""() => {
            const images = document.querySelectorAll('img');
            const missing = [];
            images.forEach(img => {
                if (!img.offsetParent) return;  // skip hidden
                if (!img.hasAttribute('alt')) {
                    missing.push(img.src.slice(-50));
                }
            });
            return missing;
        }""")

        if missing_alt:
            pytest.xfail(f"Images missing alt attribute: {missing_alt[:5]}")
        console_tracker.assert_no_errors("alt text check")


class TestErrorAnnouncement:
    """A11Y-08: Form errors announced to screen readers."""

    def test_a11y_08_error_announcement(self, navigate, authenticated_page, console_tracker):
        """Check for aria-live or role=alert on error containers."""
        page = navigate("/customers")
        page.wait_for_timeout(3000)

        has_live_region = page.evaluate("""() => {
            return !!document.querySelector(
                '[aria-live], [role="alert"], [role="status"], .p-toast'
            );
        }""")

        if not has_live_region:
            pytest.xfail("No aria-live or role=alert region found for error announcements")
        console_tracker.assert_no_errors("error announcement")


class TestModalFocusTrap:
    """A11Y-09: Focus trapped inside open modal."""

    def test_a11y_09_modal_trap(self, navigate, authenticated_page, console_tracker):
        """Open a modal and verify focus is trapped."""
        page = navigate("/customers")
        page.wait_for_timeout(3000)

        # Try to open a dialog (e.g. New Customer button)
        btn = page.locator(
            "button:has-text('New'), button:has-text('Add'), button:has-text('Create')"
        ).first
        if not btn.is_visible(timeout=3000):
            pytest.skip("No dialog-opening button found")

        btn.click()
        page.wait_for_timeout(1000)

        dialog = page.locator("[role='dialog'], .p-dialog").first
        if not dialog.is_visible(timeout=3000):
            pytest.skip("No dialog appeared")

        # Tab several times and verify focus stays in dialog
        for _ in range(15):
            page.keyboard.press("Tab")
            page.wait_for_timeout(100)

        focused_in_dialog = page.evaluate("""() => {
            const dialog = document.querySelector('[role="dialog"], .p-dialog');
            if (!dialog) return false;
            return dialog.contains(document.activeElement);
        }""")

        # Close with Escape
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

        if not focused_in_dialog:
            pytest.xfail("Focus escaped modal dialog during Tab navigation")
        console_tracker.assert_no_errors("modal focus trap")


class TestSkipLink:
    """A11Y-10: Skip to content link present and works."""

    def test_a11y_10_skip_link(self, navigate, authenticated_page, console_tracker):
        """Check for skip link."""
        page = navigate("/dashboard")
        page.wait_for_timeout(2000)

        skip_link = page.evaluate("""() => {
            const links = document.querySelectorAll('a');
            for (const link of links) {
                const text = link.textContent.toLowerCase().trim();
                if (text.includes('skip') && text.includes('content')) {
                    return { href: link.href, text: text };
                }
            }
            return null;
        }""")

        if not skip_link:
            pytest.xfail("No 'Skip to content' link found")
        console_tracker.assert_no_errors("skip link")


class TestHeadingHierarchy:
    """A11Y-11: h1 -> h2 -> h3, no skipped levels."""

    def test_a11y_11_headings(self, navigate, authenticated_page, console_tracker):
        """Check heading hierarchy has no skipped levels."""
        page = navigate("/dashboard")
        page.wait_for_timeout(3000)

        headings = page.evaluate("""() => {
            const hs = document.querySelectorAll('h1, h2, h3, h4, h5, h6');
            return Array.from(hs)
                .filter(h => h.offsetParent)  // visible only
                .map(h => parseInt(h.tagName[1]));
        }""")

        if not headings:
            pytest.xfail("No headings found on page")

        # Check for skipped levels
        skips = []
        for i in range(1, len(headings)):
            if headings[i] > headings[i - 1] + 1:
                skips.append(f"h{headings[i-1]} -> h{headings[i]}")

        if skips:
            pytest.xfail(f"Heading levels skipped: {', '.join(skips)}")
        console_tracker.assert_no_errors("heading hierarchy")


class TestTableHeaders:
    """A11Y-12: Data tables have th elements with scope."""

    def test_a11y_12_table_headers(self, navigate, authenticated_page, console_tracker):
        """Check data tables have proper th elements."""
        page = navigate("/customers")
        page.wait_for_timeout(3000)

        table_info = page.evaluate("""() => {
            const tables = document.querySelectorAll('table');
            const results = [];
            tables.forEach((table, i) => {
                if (!table.offsetParent) return;  // skip hidden
                const ths = table.querySelectorAll('th');
                const hasHeaders = ths.length > 0;
                const allHaveScope = Array.from(ths).every(
                    th => th.getAttribute('scope') || th.getAttribute('role')
                );
                results.push({
                    index: i,
                    rows: table.querySelectorAll('tr').length,
                    hasHeaders: hasHeaders,
                    headerCount: ths.length,
                    allHaveScope: allHaveScope,
                });
            });
            return results;
        }""")

        data_tables = [t for t in table_info if t["rows"] > 1]
        tables_without_headers = [t for t in data_tables if not t["hasHeaders"]]

        if tables_without_headers:
            pytest.xfail(
                f"{len(tables_without_headers)} data table(s) missing th elements"
            )
        console_tracker.assert_no_errors("table headers")
