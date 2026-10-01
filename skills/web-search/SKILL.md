---
name: web-search
description: "Search the web and extract content. Use when user asks for current info, facts, or URLs."
triggers:
  - "search for"
  - "look up"
  - "find information about"
  - "what is"
  - "who is"
version: 1.0.0
---

# Web Search

## Procedure
1. Determine search query from user's request
2. Call web_search tool with query
3. Extract top results, summarize for user
4. If deep content needed, use web_extract on specific URLs

## Pitfalls
- Don't search for code syntax errors — use terminal grep instead
- Rate limits: max 5 searches per turn
- SearXNG must be running locally for self-hosted search
