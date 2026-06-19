// Tour catalog — single source of truth for role-aware in-app tours.
//
// IMPORTANT: every step's anchor must be ALWAYS-VISIBLE on the dashboard.
// Sidebar leaf nav-* links live inside collapsed PanelMenu categories;
// driver.js will happily "highlight" an offscreen element and the popover
// floats over the wrong region (Doug caught this on the first deploy).
//
// Safe anchor surfaces (verified visible on /dashboard at all viewports):
//   - AppTopbar.vue:           help-button, topbar-new-job, topbar-new-estimate
//   - AppSidebar.vue:          nav-dashboard (pinned), tour-replay (footer)
//   - DashboardView.vue:       dashboard-quick-actions, dash-{service-call,
//                              new-job,new-estimate,new-customer,open-dispatch}
//
// Anything else needs a new data-tour attribute added to that view first.
// The check_tour_anchors.mjs CI gate fails the build on missing anchors.

const OWNER_TOUR = {
  id: 'owner-getting-started',
  role: 'owner',
  version: 2,
  autoLaunch: true,
  steps: [
    {
      anchor: '[data-tour="nav-dashboard"]',
      title: "Welcome — let's set up your shop",
      description: 'Your dashboard shows everything happening today: open jobs, pending invoices, and items that need your attention. This tour walks you through the basics.',
      helpArticleId: 'owner-getting-started',
    },
    {
      anchor: '[data-tour="dashboard-quick-actions"]',
      title: 'Quick actions',
      description: 'These five buttons cover the everyday office work: service call, new job, new estimate, new customer, and dispatch. Most days you start from here.',
    },
    {
      anchor: '[data-tour="dash-new-customer"]',
      title: 'Add your first customer',
      description: 'Start with a real customer — maybe a friend or a repeat. You can also bulk-import from QuickBooks or a CSV.',
      helpArticleId: 'customers',
    },
    {
      anchor: '[data-tour="dash-new-job"]',
      title: 'Schedule a job',
      description: 'Pick a customer, choose a service, assign a tech. The job lands on the dispatch board and on the assigned tech\'s mobile route.',
      helpArticleId: 'jobs',
    },
    {
      anchor: '[data-tour="help-button"]',
      title: 'Find help anytime',
      description: 'The question-mark button opens the help drawer with searchable articles. The "Take the tour" link in the sidebar replays this tour anytime.',
    },
  ],
};

const ADMIN_TOUR = {
  id: 'admin-getting-started',
  role: 'admin',
  version: 2,
  autoLaunch: true,
  steps: [
    {
      anchor: '[data-tour="nav-dashboard"]',
      title: 'Welcome to your console',
      description: 'The dashboard surfaces today\'s priorities — jobs scheduled, invoices ready to send, items waiting on someone.',
    },
    {
      anchor: '[data-tour="dashboard-quick-actions"]',
      title: 'Quick actions',
      description: 'Service call, new job, new estimate, new customer, and dispatch — the five most common things you\'ll do from the dashboard.',
      helpArticleId: 'jobs',
    },
    {
      anchor: '[data-tour="topbar-new-job"]',
      title: 'Create a job from anywhere',
      description: 'The "+ Job" button in the top bar works from any page. Pick the customer, choose a service, assign a tech, save.',
      helpArticleId: 'jobs',
    },
    {
      anchor: '[data-tour="help-button"]',
      title: 'Help is one click away',
      description: 'The help drawer has searchable articles for every part of the app — customers, jobs, invoices, dispatch, billing, and more.',
      helpArticleId: 'welcome',
    },
  ],
};

const DISPATCHER_TOUR = {
  id: 'dispatcher-daily-flow',
  role: 'dispatcher',
  version: 2,
  autoLaunch: true,
  steps: [
    {
      anchor: '[data-tour="nav-dashboard"]',
      title: 'Your morning view',
      description: 'Open jobs, today\'s schedule, and anything that needs your attention — all on the dashboard.',
      helpArticleId: 'dispatcher-daily-flow',
    },
    {
      anchor: '[data-tour="dash-open-dispatch"]',
      title: 'Open the dispatch board',
      description: 'Jump straight to dispatch from here. Drag jobs between technicians, drop on a different time, customer gets an SMS automatically.',
      helpArticleId: 'dispatch',
    },
    {
      anchor: '[data-tour="dashboard-quick-actions"]',
      title: 'Walk-in or phone-in?',
      description: 'New work coming in mid-day? "+ Service Call" and "+ New Job" create the record and drop it on the board in seconds.',
    },
    {
      anchor: '[data-tour="help-button"]',
      title: 'Need a hand?',
      description: 'The help drawer covers every part of the dispatch flow. Replay this tour anytime from the sidebar.',
    },
  ],
};

const TECH_TOUR = {
  id: 'tech-mobile-flow',
  role: 'technician',
  // v3: padded from 2→5 steps. Bumping forces a re-run for any tech who
  // completed v2 with only the two original steps so they see the new
  // quick-actions / quotes / new-job steps.
  version: 3,
  autoLaunch: true,
  steps: [
    {
      anchor: '[data-tour="nav-dashboard"]',
      title: "Today's road",
      description: "Your jobs in scheduled order. Tap one to see customer, address, and notes from dispatch.",
      helpArticleId: 'tech-mobile-flow',
    },
    {
      anchor: '[data-tour="dashboard-quick-actions"]',
      title: 'Quick actions on every screen',
      description: "Need to take down a service call between jobs? Add an estimate? These buttons work the same on desktop and mobile.",
    },
    {
      anchor: '[data-tour="dash-new-estimate"]',
      title: 'Quotes on the truck',
      description: 'Good / Better / Best in 30 seconds. Hand the phone to the customer, get a signature, the job is created automatically.',
      helpArticleId: 'tech-mobile-flow',
    },
    {
      anchor: '[data-tour="topbar-new-job"]',
      title: 'Add work from the field',
      description: "Walk-in repair while you're on-site? Add a job and it lands on dispatch with you already assigned.",
      helpArticleId: 'jobs',
    },
    {
      anchor: '[data-tour="help-button"]',
      title: 'Closeouts, history, help',
      description: 'When the work is done, the closeout sheet captures parts, hours, and signature. Tap the help button for the full walkthrough.',
      helpArticleId: 'tech-mobile-flow',
    },
  ],
};

export const TOURS = [OWNER_TOUR, ADMIN_TOUR, DISPATCHER_TOUR, TECH_TOUR];

export function findTour(tourId) {
  return TOURS.find((t) => t.id === tourId) || null;
}

export function toursForRole(role, { enabledModules = null } = {}) {
  const r = String(role || '').toLowerCase();
  const match = (tRole) => {
    const lt = String(tRole || 'all').toLowerCase();
    if (lt === 'all') return true;
    if (lt === r) return true;
    if ((lt === 'tech' && r === 'technician') || (lt === 'technician' && r === 'tech')) return true;
    return false;
  };
  return TOURS.filter((t) => match(t.role)).filter((t) => {
    if (!t.module) return true;
    if (!enabledModules) return true;
    return enabledModules[t.module] === true;
  });
}

export function defaultTourIdForRole(role) {
  const list = toursForRole(role);
  return list.length ? list[0].id : null;
}
