// Widget kit registry — maps the widget type names used in game definition
// layout_json to the actual Vue components. The GamePlayer reads layout_json,
// looks up each widget by type, and renders it.
//
// To add a new widget type:
//   1. Create a new .vue file in this folder
//   2. Add it to the import + map below
//   3. Use its type name in any game definition's layout_json

import HeartBar from './HeartBar.vue';
import ProgressBar from './ProgressBar.vue';
import Counter from './Counter.vue';
import EventFeed from './EventFeed.vue';
import BigButton from './BigButton.vue';

export const widgetMap = {
  HeartBar,
  ProgressBar,
  Counter,
  EventFeed,
  BigButton,
};

export { HeartBar, ProgressBar, Counter, EventFeed, BigButton };
