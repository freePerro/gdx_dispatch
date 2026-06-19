# Help article style guide

## Frontmatter (required fields)

```yaml
---
title: Create a job
role: dispatcher        # owner | admin | dispatcher | technician | all
tags: jobs, dispatch    # comma-separated
related: customers, dispatch
module: jobs            # optional — used by tour module-skip
video_url:              # optional Loom/YouTube URL
lang: en                # optional, default en
---
```

## Writing

- **Active voice, "you" not "the user."** "Click Save" not "the user should click Save."
- **One H1 per article** — matches frontmatter `title`. Subsequent sections use `##`.
- **Short paragraphs.** Two sentences max per paragraph for skimmability.
- **Lists over prose** when 3+ items.
- **No jargon without definition.** "Closeout" gets defined the first time it appears.

## Screenshots

- Drop into `src/help/assets/<article-slug>/<n>.png`.
- Max 800px wide; PNG or WebP.
- Always include alt text: `![Alt text describing the screenshot](/help/assets/jobs/create-1.png)`.
- Annotate with red boxes/arrows only when "look at this specific thing" is the point.

## Video

- Loom or YouTube. Use embed via:
  ```
  video_url: https://www.loom.com/share/abc123
  ```
- The drawer will render an inline player at the top of the article if present.

## Related links

Every article ends with 2–3 `related:` slugs. The drawer renders them at the bottom and as in-tour "Learn more" deep-links.

## Tone reference

- Housecall Pro voice — direct, friendly, no-nonsense.
- Skip marketing language. ("Streamline your workflow" → "Get jobs done faster.")
- Skip imperatives where context makes them obvious. ("Then, click the Save button to save your changes." → "Click Save.")
