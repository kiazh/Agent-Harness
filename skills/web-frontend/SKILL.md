---
name: web-frontend
description: Build and review websites and landing pages — semantic HTML, CSS layout,
  responsive design, accessibility, performance, forms, SEO, and browser testing
triggers:
- website
- webpage
- web app
- landing page
- frontend
- web design
- html
- css
- responsive
- homepage
version: 1.0.0
schema_version: 2
skill_id: web-frontend
task_families:
- web-build
- frontend-review
capabilities:
- layout
- responsive-ui
- accessibility
- performance
aliases:
- website
- websites
- web-app
- webapp
- landing-page
- frontend
- html-css
- responsive-site
- webpage
stack:
  languages:
  - html
  - css
  - javascript
  - typescript
  frameworks: []
  independent: true
preconditions:
- A page or site goal and its primary audience
- A place to write static assets or a frontend project
required_tools: []
exclusions:
- Backend service design with no browser-facing surface
- Native mobile app UI rather than a website
- Copywriting or copy-only review with no markup, layout, or code change
- Landing page copy
sections:
- id: plan-the-build
  heading: Plan the Build
  stage: plan
  requires_sections: []
- id: structure-and-layout
  heading: Structure and Layout
  stage: implement
  requires_sections:
  - plan-the-build
- id: responsive-design
  heading: Responsive Design
  stage: implement
  requires_sections:
  - structure-and-layout
- id: accessibility
  heading: Accessibility
  stage: review
  requires_sections:
  - structure-and-layout
- id: forms-and-seo
  heading: Forms and SEO
  stage: implement
  requires_sections:
  - structure-and-layout
- id: performance
  heading: Performance
  stage: verify
  requires_sections:
  - responsive-design
- id: testing-and-delivery
  heading: Testing and Delivery
  stage: deliver
  requires_sections:
  - accessibility
  - performance
metadata_source: authored
---

## Plan the Build

Start from the page's job, not from a framework. Write one sentence for the
audience, one for the primary action, and one for what must be visible without
scrolling. List the sections in reading order (hero, proof, features, pricing,
contact, footer) and mark which are real content versus placeholder. Decide
whether the deliverable is a single static page, a few linked pages, or a
component inside an existing app. Prefer plain HTML, CSS, and JavaScript for
static pages; add a framework only when the existing project already uses one.
Collect brand inputs (logo, colors, type choices, imagery) before styling so the
first pass is coherent rather than a sequence of reworks.

## Structure and Layout

Use semantic landmarks — `header`, `nav`, `main`, `section`, `article`,
`aside`, and `footer` — plus exactly one `h1` that names the page. Keep heading
levels sequential so assistive technology can build an outline. Lay out with
normal flow first, then CSS Grid for two-dimensional regions and Flexbox for
one-dimensional rows. Use logical properties (`margin-inline`, `padding-block`)
and a small spacing scale (for example 4/8/16/24/32/48/64) defined as custom
properties. Constrain reading width (roughly 60–75 characters) and use
`max-width` plus auto inline margins for centered content. Avoid absolute
positioning for primary layout; reserve it for decorative overlays that never
carry content.

## Responsive Design

Design mobile-first: the base styles target a narrow viewport, and
`min-width` media queries add columns as space allows. Use fluid type and
spacing with `clamp()` rather than a stack of breakpoint overrides. Let images
shrink with `max-width: 100%`, set `aspect-ratio` to prevent layout shift, and
serve `srcset`/`sizes` variants for large hero art. Test at 320 px, 768 px,
1024 px, and a wide desktop width, plus 200% browser zoom. Prefer intrinsic
layout (Grid `auto-fit`/`minmax`, `flex-wrap`) over device-specific fixed
widths. Keep tap targets at least 44×44 CSS pixels and avoid hover-only
affordances for primary navigation.

## Accessibility

Every image needs a purpose: meaningful `alt` text or `alt=""` when decorative.
Associate each input with a visible `label`, keep focus visible, and preserve a
logical tab order. Verify text contrast against WCAG AA (4.5:1 for body text,
3:1 for large text and UI boundaries) and never rely on color alone to convey
state. Use native elements (`button`, `a`, `details`) before ARIA; when ARIA is
unavoidable, match the role's required states and keyboard behavior. Provide a
skip link, landmark labels, and a page language. Respect
`prefers-reduced-motion` and avoid autoplaying media with sound.

## Forms and SEO

Give every field a `name`, an appropriate `type`/`autocomplete`, and inline
validation messages tied to the field with `aria-describedby`. Show errors next
to the field, keep entered values on failure, and disable submit only while a
request is genuinely in flight. Set a descriptive `<title>`, a meta
description, a canonical URL, Open Graph tags, and one clear `h1`. Add
structured data only when it matches visible content. Emit a `sitemap.xml` and
`robots.txt` for multi-page sites, use descriptive link text, and keep URLs
stable and lowercase.

## Performance

Budget the first load: compress HTML/CSS/JS, preload the hero asset and primary
font, and defer non-critical scripts. Use modern formats (WebP/AVIF) with
explicit dimensions, lazy-load below-the-fold media, and avoid layout thrash
from late-loading embeds. Inline critical CSS for very small pages; otherwise
ship one cacheable stylesheet. Measure with Lighthouse and the Performance
panel, and treat Core Web Vitals (LCP, CLS, INP) as the acceptance signal rather
than subjective smoothness. Remove unused CSS and third-party scripts before
adding more client-side state.

## Testing and Delivery

Validate HTML, run an automated accessibility scan (axe or Lighthouse), and
click through every primary action with a keyboard only. Check links, form
submission success and failure, empty states, and long-content wrapping.
Capture screenshots at the responsive widths above and compare against the
plan. Deliver the source, a short README with run/build commands, and the
measured performance/accessibility results. If deployment is requested, keep
secrets out of the bundle and verify the production URL after release.
