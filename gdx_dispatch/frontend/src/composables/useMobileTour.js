/**
 * Sprint tech_mobile Phase 4.5 — driver.js-backed in-app tour.
 *
 * Two role-aware flows:
 *   - tech: today's route → on-my-way → arrival → quote → close-out
 *   - dispatcher: dispatch threads → unread filter → reassign
 *
 * On mobile (<600px) we drop driver.js's tooltip mode and render
 * full-screen step cards (industry pattern from ServiceTitan / Housecall
 * — tooltips collide with element chrome on small screens).
 *
 * State persists in `localStorage[gdx_tour_done.<role>]` for v1; a
 * server-side `user.seen_features` flag is reserved for v2 so a tech
 * who reinstalls / clears localStorage doesn't re-tour.
 */
import { driver } from 'driver.js'
import 'driver.js/dist/driver.css'

const KEY = (role) => `gdx_tour_done.${role}`

const TECH_STEPS = [
  {
    element: '.today-route',
    popover: {
      title: 'Today\'s route',
      description: 'Your jobs in scheduled order. Each card shows the customer, address, and ETA from the previous stop.',
    },
  },
  {
    element: '.job-actions .p-button:first-child',
    popover: {
      title: 'On my way',
      description: 'Tap when you head out. Dispatch sees your status change instantly.',
    },
  },
  {
    element: '.job-actions',
    popover: {
      title: 'Quote on the truck',
      description: 'Build a Good / Better / Best quote, hand the phone to the customer, capture their signature — done in 30 seconds.',
    },
  },
  {
    element: '.offline-banner, .today-route',
    popover: {
      title: 'Works offline',
      description: 'Lost signal? Your work keeps saving locally and syncs the moment you reconnect.',
    },
  },
]

const DISPATCH_STEPS = [
  {
    element: '.dispatch-page',
    popover: {
      title: 'Dispatch on your phone',
      description: 'Active per-job chat threads, unread first. Tap any thread to read or reply.',
    },
  },
  {
    element: '.thread-item.has-unread, .thread-item:first-child',
    popover: {
      title: 'Unread threads jump to the top',
      description: 'Red border + count = a tech is waiting on you.',
    },
  },
]

function _isMobile() {
  return typeof window !== 'undefined' && window.innerWidth < 600
}

export function useMobileTour() {
  function hasSeen(role) {
    try { return localStorage.getItem(KEY(role)) === '1' } catch { return false }
  }
  function markSeen(role) {
    try { localStorage.setItem(KEY(role), '1') } catch {}
  }

  function start(role = 'tech', { force = false } = {}) {
    if (!force && hasSeen(role)) return
    const steps = role === 'dispatcher' ? DISPATCH_STEPS : TECH_STEPS
    const driverObj = driver({
      showProgress: true,
      animate: true,
      // Mobile: full-width popover, no element-anchor (driver.js still
      // highlights the element but the card overlays the bottom of the
      // screen rather than positioning around it).
      popoverClass: _isMobile() ? 'gdx-tour-mobile' : 'gdx-tour-desktop',
      stagePadding: 4,
      onDestroyStarted: () => {
        markSeen(role)
        driverObj.destroy()
      },
      steps: steps.filter(s => {
        if (typeof document === 'undefined') return true
        if (!s.element) return true
        try { return !!document.querySelector(s.element) } catch { return false }
      }),
    })
    if (driverObj.getActiveSteps?.().length === 0) {
      // No targets in DOM — nothing to tour.
      return
    }
    driverObj.drive()
  }

  function reset(role = 'tech') {
    try { localStorage.removeItem(KEY(role)) } catch {}
  }

  return { start, reset, hasSeen, markSeen }
}
