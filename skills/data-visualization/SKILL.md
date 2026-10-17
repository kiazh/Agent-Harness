---
name: data-visualization
description: Data visualization — chart selection, encoding, color and accessibility, matplotlib, dashboards,
  annotation, honest axes
triggers:
- visualization
- chart
- plot
- dataviz
- matplotlib
- dashboard
- d3
version: 1.0.0
schema_version: 2
skill_id: data-visualization
task_families:
- data-analysis
capabilities:
- accessibility
- layout
aliases:
- dataviz
- charts
- plotting
- matplotlib
- dashboards
- dashboard
- website-charts
- website
stack:
  languages:
  - python
  frameworks:
  - matplotlib
  independent: false
preconditions:
- A dataset or result set to present visually
- 'A target medium: notebook, report, or dashboard'
required_tools:
- shell
exclusions:
- Generating imagery with diffusion models
- Purely textual summaries with no figure
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: pitfalls
  heading: Pitfalls
  stage: review
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
- id: key-resources
  heading: Key Resources
  stage: inspect
  requires_sections: []
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Data Visualization

## Core Skills

| Skill | Practice |
|---|---|
| Chart selection | Match the chart to the comparison: trend, parts, distribution, correlation |
| Encoding | Position and length before area and color for accuracy |
| Honest axes | Bar charts start at zero; label units and direction |
| Color | Sequential, diverging or categorical palettes; colorblind-safe |
| Accessibility | Contrast, redundant encoding, alt text for every figure |
| Dashboard layout | One question per panel; context and outliers visible |
| Annotation | Point at the insight; remove chartjunk |
| Reproducibility | Generate figures from code, not manual edits |

## Pitfalls
- Truncated y-axes exaggerate small differences; say so when intended.
- Rainbow palettes mislead on ordered data and fail colorblind readers.
- Dual axes invite false correlations; prefer indexed series or small multiples.
- Pie charts with many slices communicate ranking poorly; use bars.

## Practice Project
Take one dataset and produce three figures: a trend line, a distribution, and a ranked comparison. Make each colorblind-safe with labeled axes and one clear annotation, and render all three from a single reproducible script.

## Key Resources
- [Data Viz Project](https://datavizproject.com/)
- [matplotlib docs](https://matplotlib.org/stable/)
- [ColorBrewer](https://colorbrewer2.org/)
