# Research: Arrow Key Menus in Hermes Agent and OpenCode

> Comprehensive analysis of how Hermes Agent and OpenCode implement interactive menus,
> autocomplete dropdowns, and scrollable help menus — with code examples and
> implementation patterns for building similar UIs.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Hermes Agent — Classic CLI (Python/prompt_toolkit)](#hermes-agent--classic-cli)
3. [Hermes Agent — Modern TUI (TypeScript/Ink)](#hermes-agent--modern-tui)
4. [OpenCode TUI (TypeScript/Opentui)](#opencode-tui)
5. [Autocomplete Dropdown Implementation](#autocomplete-dropdown-implementation)
6. [Arrow Key Navigation Patterns](#arrow-key-navigation-patterns)
7. [Scrollable Help Menus](#scrollable-help-menus)
8. [Selection vs Execution](#selection-vs-execution)
9. [Visual Styling Patterns](#visual-styling-patterns)
10. [Key Implementation Patterns](#key-implementation-patterns)
11. [Comparison Summary](#comparison-summary)

---

## Architecture Overview

| Aspect | Hermes Classic CLI | Hermes Modern TUI | OpenCode TUI |
|--------|-------------------|-------------------|--------------|
| **Language** | Python | TypeScript | TypeScript |
| **Framework** | prompt_toolkit | Ink (React for CLI) | Opentui + SolidJS |
| **Menu System** | `CompletionsMenu` + custom overlays | Custom Ink components | `DialogSelect` + `Autocomplete` |
| **Key Binding** | `KeyBindings` + `@kb.add()` | `useInput()` hook | `useBindings()` + keymap |
| **State Management** | Python class state | nanostores + React state | SolidJS stores |
| **Rendering** | prompt_toolkit layout | Ink reconciler | Opentui renderer |

---

## Hermes Agent — Classic CLI

### File: `hermes_cli/commands_completion.py`

The classic CLI uses **prompt_toolkit**'s built-in completion system with a custom `Completer` and `AutoSuggest`.

#### SlashCommandCompleter

```python
class SlashCommandCompleter(Completer):
    """prompt_toolkit Completer for slash commands with dynamic argument completion."""

    def __init__(self, ...):
        self._skill_bundles_provider = ...
        self._skill_commands_cache = ...

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return  # Only complete slash commands

        word = text[1:]  # Strip leading /

        # Complete command name
        if " " not in word:
            for cmd, desc in COMMANDS.items():
                if self._command_allowed(cmd) and cmd[1:].startswith(word):
                    yield self._cmd_completion(cmd[1:], word, f"/{cmd[1:]}", desc)

            # Also complete skill commands
            for cmd, info in self._iter_skill_commands().items():
                if cmd[1:].startswith(word):
                    yield self._cmd_completion(
                        cmd[1:], word, f"/{cmd[1:]}",
                        f"⚡ {info.get('description', 'Skill command')}"
                    )

        # Complete subcommands/arguments
        else:
            base_cmd = word.split()[0]
            sub_text = word[len(base_cmd):].lstrip()
            # ... dynamic argument completers
```

**Key pattern**: The completer yields `Completion` objects with `start_position=-len(word)` to replace the word under the cursor.

#### SlashCommandAutoSuggest (Ghost Text)

```python
class SlashCommandAutoSuggest(AutoSuggest):
    """Inline ghost-text for slash commands; history fallback for other input."""

    def get_suggestion(self, buffer, document):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return self._history_suggestion(buffer, document)

        parts = text.split(maxsplit=1)
        base_cmd = parts[0].lower()

        # Still typing the name: prefer the SHORTEST match
        # so /he ghosts "lp", not "heartbeat"
        if len(parts) == 1 and not text.endswith(" "):
            word = text[1:].lower()
            for cmd in sorted(COMMANDS, key=len):
                cmd_name = cmd[1:]
                if self._allowed(cmd) and cmd_name.startswith(word) and cmd_name != word:
                    return Suggestion(cmd_name[len(word):])
            return None

        # Subcommand ghost text
        sub_text = parts[1] if len(parts) > 1 else ""
        if " " not in sub_text:
            for sub in SUBCOMMANDS.get(base_cmd, ()):
                if sub.startswith(sub_text.lower()) and sub != sub_text:
                    return Suggestion(sub[len(sub_text):])
        return None
```

**Key pattern**: Ghost text shows the remaining characters of the shortest matching command. `/he` → ghosts `lp` to complete `/help`.

### File: `hermes_cli/cli_tui_mixin.py`

The TUI construction uses prompt_toolkit's layout system:

```python
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.key_binding import KeyBindings

class CLITUIMixin:
    def _build_tui(self):
        # TextArea with completer and auto-suggest
        self._input_area = TextArea(
            multiline=True,
            completer=SlashCommandCompleter(...),
            auto_suggest=SlashCommandAutoSuggest(...),
            history=FileHistory(...),
        )

        # CompletionsMenu controls the dropdown display
        self._completions_menu = CompletionsMenu(
            max_height=12,
            scroll_offset=1,
            extra_filter=Condition(lambda: self._show_completions),
        )

        # Layout with HSplit
        self._layout = Layout(
            HSplit([
                # ... transcript area
                Window(content=FormattedTextControl(...)),
                # Completions menu positioned above input
                self._completions_menu,
                # Input area
                self._input_area,
                # Status bar
                Window(content=FormattedTextControl(...)),
            ])
        )
```

### File: `hermes_cli/callbacks.py` — Clarify/Approval Menus

Interactive selection menus with arrow key navigation:

```python
def clarify_callback(cli, question, choices, multi_select=False):
    """Prompt for clarifying question through the TUI.
    Sets up interactive selection UI, blocks until user responds.
    """
    response_queue = queue.Queue()
    cli._clarify_state = {
        "question": question,
        "choices": choices,
        "selected": 0,  # Current selection index
        "multi_select": multi_select,
        "selected_indices": set() if multi_select else None,
        "response_queue": response_queue,
    }
    cli._clarify_deadline = _time.monotonic() + timeout

    # Block until user responds or timeout
    while True:
        try:
            result = response_queue.get(timeout=1)
            return result
        except queue.Empty:
            if cli._clarify_deadline - _time.monotonic() <= 0:
                break
            if hasattr(cli, "_app") and cli._app:
                cli._app.invalidate()  # Trigger re-render
```

**Key pattern**: The callback sets state on the CLI object, then blocks polling a queue. The TUI's key handler processes arrow keys to update `selected`, and Enter to push the choice to the queue.

### File: `hermes_cli/cli_modal_mixin.py` — Modal Overlays

```python
def _render_clarify_modal(self):
    """Render the clarify selection overlay."""
    state = self._clarify_state
    if not state:
        return []

    rows = []
    for i, choice in enumerate(state["choices"]):
        prefix = "▸ " if i == state["selected"] else "  "
        if state["multi_select"]:
            checkbox = "[x]" if i in state["selected_indices"] else "[ ]"
            rows.append(f"{prefix}{checkbox} {choice}")
        else:
            rows.append(f"{prefix}{choice}")

    return self._panel("Clarify", state["question"], rows)
```

---

## Hermes Agent — Modern TUI

### File: `ui-tui/src/components/overlayControls.tsx`

The `useMenu` hook — the core arrow key navigation primitive:

```typescript
export function useMenu(
  rows: MenuRowSpec[],
  onEscape: () => void,
  onKey?: (ch: string, key: Key) => boolean
): number {
  const [sel, setSel] = useState(0)

  useInput((ch, key) => {
    // Custom key handler runs first
    if (onKey?.(ch, key)) return

    if (key.escape) return onEscape()

    if (key.upArrow && sel > 0) {
      setSel(v => v - 1)
    }

    if (key.downArrow && sel < rows.length - 1) {
      setSel(v => v + 1)
    }

    if (key.return) {
      return rows[sel]?.run()
    }

    // Number key quick-select (1-9)
    const n = parseInt(ch, 10)
    if (n >= 1 && n <= rows.length) {
      return rows[n - 1]?.run()
    }
  })

  return Math.min(sel, Math.max(0, rows.length - 1))
}
```

**Key pattern**: Single `useInput` hook handles all key events. Returns the current selection index. The caller renders rows with the returned index.

### Window Offset Calculation (Scrollable Lists)

```typescript
export const windowOffset = (count: number, selected: number, visible: number) =>
  Math.max(0, Math.min(selected - Math.floor(visible / 2), count - visible))

export function windowItems<T>(items: T[], selected: number, visible: number) {
  const offset = windowOffset(items.length, selected, visible)
  return {
    items: items.slice(offset, offset + visible),
    offset
  }
}
```

**Key pattern**: Centers the selected item in the visible window. `offset = selected - floor(visible/2)`, clamped to `[0, count - visible]`.

### File: `ui-tui/src/components/overlayPrimitives.tsx`

Visual styling for menu rows:

```typescript
export function listRowStyle(t: Theme, active: boolean): { backgroundColor?: string; color?: string } {
  if (!active) return {}
  const backgroundColor = t.color.completionCurrentBg
  return { backgroundColor, color: liftForContrast(t.color.text, backgroundColor, 4.5) }
}

export function chipRowProps(t: Theme, active: boolean) {
  const row = listRowStyle(t, active)
  return { backgroundColor: row.backgroundColor, bold: active, ...(row.color ? { color: row.color } : {}) }
}

export function MenuRow({ active, index, label, t }: { active: boolean; index: number; label: string; t: Theme }) {
  const row = listRowStyle(t, active)
  return (
    <Text>
      <Text
        backgroundColor={row.backgroundColor}
        bold={active}
        color={active ? (row.color ?? t.color.label) : t.color.muted}
      >
        {active ? '▸ ' : '  '}
        {index}. {label}
      </Text>
    </Text>
  )
}
```

**Key pattern**: Only the active row gets a background color. The `▸` cursor indicates selection. Inactive rows use muted text.

### File: `ui-tui/src/hooks/useCompletion.ts`

Completion request routing:

```typescript
export function completionRequestForInput(input: string):
  | { method: 'complete.path'; params: { word: string }; replaceFrom: number }
  | { method: 'complete.slash'; params: { text: string }; replaceFrom: number; skillsOnly?: boolean }
  | null {

  const isSlashCommand = looksLikeSlashCommand(input)
  const pathWord = isSlashCommand ? null : (input.match(TAB_PATH_RE)?.[1] ?? null)

  // Inline skill reference (e.g., "clean this up with /clean")
  const inline = inlineSlashTrigger(input)
  if (inline) {
    return { method: 'complete.slash', params: { text: `/${inline.query}` }, replaceFrom: inline.start + 1, skillsOnly: true }
  }

  if (isSlashCommand) {
    return { method: 'complete.slash', params: { text: input }, replaceFrom: 1 }
  }

  if (!pathWord) return null
  return { method: 'complete.path', params: { word: pathWord }, replaceFrom: ... }
}
```

### File: `ui-tui/src/app/useInputHandlers.ts`

Global input handling with completion navigation:

```typescript
// Arrow key navigation through completions
if (cState.completions.length && cState.input && cState.historyIdx === null && (key.upArrow || key.downArrow)) {
  const len = cState.completions.length
  cActions.setCompIdx(i => (key.upArrow ? (i - 1 + len) % len : (i + 1) % len))
  return
}

// Tab accepts completion
if (key.tab && cState.completions.length) {
  const row = cState.completions[cState.compIdx]
  if (row?.text) {
    cActions.setInput(applyCompletion(cState.input, row.text, cState.compReplace))
  }
  return
}
```

**Key pattern**: Wrap-around navigation (`(i - 1 + len) % len`). Tab accepts the current completion.

### File: `ui-tui/src/app/slash/fuzzyScore.ts`

Description-aware fuzzy scoring:

```typescript
export function scoreSlashMenuItem(item: SlashScoreItem, query: string): number {
  const commandFields = [item.id, item.label ?? '', ...(item.aliases ?? [])]
    .filter(Boolean)
    .flatMap(tokenizeSearchText)
  const descriptionFields = tokenizeSearchText(item.description ?? '')

  // Tier 0: exact match on id/label/alias
  // Tier 1: prefix match
  // Tier 2: substring match
  // Tier 3+: description match (offset by 3)
  return Math.min(scoreFields(commandFields, query, 0), scoreFields(descriptionFields, query, 3))
}
```

**Key pattern**: Multi-tier scoring — exact (0), prefix (1), substring (2), description (3+). This lets `/summary` surface a command whose description mentions "summaries".

---

## OpenCode TUI

### File: `packages/tui/src/component/prompt/autocomplete.tsx`

The autocomplete dropdown component (SolidJS):

```typescript
export function Autocomplete(props: {
  value: string
  input: () => TextareaRenderable
  anchor: () => BoxRenderable
  ref: (ref: AutocompleteRef) => void
  // ...
}) {
  const [store, setStore] = createStore({
    index: 0,        // Trigger position
    selected: 0,     // Selected option index
    visible: false as false | "@" | "/",
    input: "keyboard" as "keyboard" | "mouse",
  })

  // Arrow key navigation
  function move(direction: -1 | 1) {
    if (!store.visible) return
    if (!options().length) return
    let next = store.selected + direction
    if (next < 0) next = options().length - 1  // Wrap around
    if (next >= options().length) next = 0
    moveTo(next)
  }

  function moveTo(next: number) {
    setStore("selected", next)
    if (!scroll) return
    const viewportHeight = Math.min(height(), options().length)
    const scrollBottom = scroll.scrollTop + viewportHeight
    if (next < scroll.scrollTop) {
      scroll.scrollBy(next - scroll.scrollTop)
    } else if (next + 1 > scrollBottom) {
      scroll.scrollBy(next + 1 - scrollBottom)
    }
  }

  function select() {
    const selected = options()[store.selected]
    if (!selected) return
    hide()
    selected.onSelect?.()
  }

  // Key bindings
  useBindings(() => ({
    target: props.input,
    enabled: () => Boolean(store.visible),
    commands: [
      { name: "prompt.autocomplete.prev", run() { move(-1) } },
      { name: "prompt.autocomplete.next", run() { move(1) } },
      { name: "prompt.autocomplete.hide", run() { hide() } },
      { name: "prompt.autocomplete.select", run() { select() } },
      { name: "prompt.autocomplete.complete", run() {
        const selected = options()[store.selected]
        if (selected?.isDirectory) { expandDirectory(); return }
        select()
      }},
    ],
  }))

  // Render
  return (
    <box visible={store.visible !== false} position="absolute" zIndex={100}>
      <scrollbox ref={r => scroll = r} height={height()}>
        <Index each={options()}>
          {(option, index) => (
            <box
              backgroundColor={index === store.selected ? theme.primary : undefined}
              onMouseMove={() => { if (store.input === "mouse") moveTo(index) }}
              onMouseDown={() => moveTo(index)}
              onMouseUp={() => select()}
            >
              <text fg={index === store.selected ? selectedForeground(theme) : theme.text}>
                {option().display}
              </text>
            </box>
          )}
        </Index>
      </scrollbox>
    </box>
  )
}
```

**Key pattern**: SolidJS reactive store drives the selected index. The `moveTo` function handles both selection and scroll positioning. Mouse and keyboard input are tracked separately.

### File: `packages/tui/src/ui/dialog-select.tsx`

The full-featured dialog select component:

```typescript
export function DialogSelect<T>(props: DialogSelectProps<T>) {
  const [store, setStore] = createStore({
    selected: 0,
    filter: "",
    input: "keyboard" as "keyboard" | "mouse",
  })

  // Fuzzy filter
  const filtered = createMemo(() => {
    if (props.skipFilter || props.renderFilter === false)
      return props.options.filter(x => x.disabled !== true)
    const needle = store.filter.toLowerCase()
    if (!needle) return props.options
    return fuzzysort.go(needle, props.options, {
      keys: ["title", "category"],
      scoreFn: (r) => r[0].score * 2 + r[1].score,  // Title matches weighted 2x
    }).map(x => x.obj)
  })

  // Grouped display
  const grouped = createMemo(() => {
    if (props.flat && store.filter.length > 0) return [["", filtered()]]
    return pipe(filtered(), groupBy(x => x.category ?? ""), entries())
  })

  // Navigation
  function move(direction: number) {
    if (props.locked) return
    let next = store.selected + direction
    if (next < 0) next = flat().length - 1
    if (next >= flat().length) next = 0
    moveTo(next, true)
  }

  function moveTo(next: number, center = false, preserve = true) {
    setStore("selected", next)
    scrollToSelection(center)
  }

  function scrollToSelection(center: boolean) {
    // Locate row by position, scroll to make it visible
    let remaining = store.selected
    let index = 0
    for (const [category, options] of grouped()) {
      if (category) index++
      if (remaining < options.length) { index += remaining; break }
      index += options.length
      remaining -= options.length
    }
    const target = scroll.getChildren()[index]
    if (!target) return
    const y = target.y - scroll.y
    if (center) {
      scroll.scrollBy(y - Math.floor(scroll.height / 2))
    } else {
      if (y >= scroll.height) scroll.scrollBy(y - scroll.height + 1)
      if (y < 0) scroll.scrollBy(y)
    }
  }

  // Key bindings
  useBindings(() => ({
    commands: [
      { name: "dialog.select.prev", run() { move(-1) } },
      { name: "dialog.select.next", run() { move(1) } },
      { name: "dialog.select.page_up", run() { move(-10) } },
      { name: "dialog.select.page_down", run() { move(10) } },
      { name: "dialog.select.home", run() { moveTo(0) } },
      { name: "dialog.select.end", run() { moveTo(flat().length - 1) } },
      { name: "dialog.select.submit", run: submit },
    ],
  }))

  // Render with grouped categories
  return (
    <box>
      <box>
        <text bold>{props.title}</text>
        <input
          onInput={e => setStore("filter", e)}
          placeholder={props.placeholder ?? "Search"}
        />
      </box>
      <scrollbox ref={r => scroll = r} maxHeight={height()}>
        <For each={grouped()}>
          {([category, options]) => (
            <>
              <Show when={category}>
                <text fg={theme.accent} bold>{category}</text>
              </Show>
              <For each={options}>
                {(option) => (
                  <box
                    backgroundColor={isDeepEqual(option.value, selected()?.value) ? theme.primary : undefined}
                    onMouseOver={() => { if (store.input === "mouse") moveTo(index) }}
                    onMouseDown={() => moveTo(index)}
                    onMouseUp={() => { option.onSelect?.(dialog) }}
                  >
                    <text bold={active}>{option.title}</text>
                    <text fg={theme.textMuted}>{option.description}</text>
                  </box>
                )}
              </For>
            </>
          )}
        </For>
      </scrollbox>
      <box>
        {/* Footer with action hints */}
      </box>
    </box>
  )
}
```

**Key pattern**: Full-featured dialog with fuzzy filtering, grouped categories, keyboard + mouse navigation, and action buttons in the footer.

### File: `packages/tui/src/ui/dialog.tsx`

The dialog overlay system:

```typescript
export function Dialog(props: ParentProps<{
  size?: "medium" | "large" | "xlarge"
  onClose: () => void
}>) {
  const dimensions = useTerminalDimensions()
  const width = () => {
    if (props.size === "xlarge") return 116
    if (props.size === "large") return 88
    return 60
  }

  return (
    <box
      width={dimensions().width}
      height={dimensions().height}
      alignItems="center"
      position="absolute"
      zIndex={3000}
      backgroundColor={RGBA.fromInts(0, 0, 0, 150)}  // Semi-transparent backdrop
    >
      <box
        width={width()}
        maxWidth={dimensions().width - 2}
        backgroundColor={theme.backgroundPanel}
        paddingTop={1}
      >
        {props.children}
      </box>
    </box>
  )
}
```

**Key pattern**: Dialogs are absolutely positioned overlays with a semi-transparent backdrop. The `zIndex` ensures they stack above the main content.

### File: `packages/tui/src/component/command-palette.tsx`

Command palette using DialogSelect:

```typescript
export function CommandPaletteDialog() {
  const keymap = useOpencodeKeymap()
  const entries = useKeymapSelector((keymap) => {
    const reachable = keymap.getCommandEntries({
      namespace: "palette",
      visibility: "reachable",
      filter: isVisiblePaletteCommand,
    })
    return reachable.map(entry => ({
      ...entry,
      bindings: registeredBindings.get(entry.command.name) ?? entry.bindings,
    }))
  })

  const options = createMemo(() =>
    entries().map(entry => ({
      title: entry.command.title,
      description: entry.command.desc,
      category: entry.command.category,
      footer: formatKeyBindings(entry.bindings, config),
      value: entry.command.name,
      onSelect: (dialog) => {
        dialog.clear()
        keymap.dispatchCommand(entry.command.name)
      },
    }))
  )

  return <DialogSelect title="Commands" options={options()} />
}
```

### File: `packages/tui/src/keymap.tsx`

The keymap system:

```typescript
export function createOpencodeModeStack(keymap: OpenTuiKeymap) {
  const stack: { id: symbol; mode: string }[] = []

  const update = () => {
    keymap.setData(OPENCODE_MODE_KEY, stack.at(-1)?.mode ?? OPENCODE_BASE_MODE)
  }

  return {
    current() { return stack.at(-1)?.mode ?? OPENCODE_BASE_MODE },
    push(mode: string) {
      const id = Symbol(mode)
      stack.push({ id, mode })
      update()
      return () => {  // Returns cleanup function
        const index = stack.findIndex(item => item.id === id)
        if (index !== -1) stack.splice(index, 1)
        update()
      }
    },
  }
}
```

**Key pattern**: Mode stack allows nested modal contexts. When a dialog opens, it pushes a mode; when it closes, the mode is popped. Key bindings are enabled/disabled based on the current mode.

---

## Autocomplete Dropdown Implementation

### Trigger Detection

| System | Trigger | Detection |
|--------|---------|-----------|
| Hermes Classic | `/` prefix | `text.startswith("/")` |
| Hermes TUI | `/` at position 0 or after whitespace | `looksLikeSlashCommand(input)` + `inlineSlashTrigger(input)` |
| OpenCode | `@` or `/` | `mentionTriggerIndex(value, offset)` for `@`, `value.startsWith("/")` for `/` |

### Filtering Strategy

**Hermes Classic** (Python):
```python
def _prefix_completions(rows, partial, *, skip_exact=True):
    lowered = partial.lower()
    for name, meta in rows:
        if name.startswith(lowered) and not (skip_exact and name == lowered):
            yield _completion(name, partial, name, meta)
```

**OpenCode** (TypeScript):
```typescript
const fuzzied = fuzzysort.go(searchValue, options, {
  keys: [obj => obj.value ?? obj.display, "description"],
  threshold: store.visible === "@" ? 0.5 : 0,
  limit: 10,
  scoreFn: (objResults) => {
    let score = objResults.score
    if (objResults[0].target.startsWith(store.visible + searchValue)) score *= 2
    const frecencyScore = objResults.obj.path ? frecency.getFrecency(objResults.obj.path) : 0
    return score * (1 + frecencyScore)
  },
})
```

**Hermes TUI** (TypeScript):
```typescript
// Multi-tier scoring: exact (0), prefix (1), substring (2), description (3+)
export function scoreSlashMenuItem(item: SlashScoreItem, query: string): number {
  const commandFields = [item.id, item.label ?? '', ...(item.aliases ?? [])]
    .flatMap(tokenizeSearchText)
  const descriptionFields = tokenizeSearchText(item.description ?? '')
  return Math.min(scoreFields(commandFields, query, 0), scoreFields(descriptionFields, query, 3))
}
```

### Rendering Position

**Hermes Classic**: prompt_toolkit's `CompletionsMenu` handles positioning automatically.

**Hermes TUI**: Custom positioning above the input area.

**OpenCode**:
```typescript
const position = createMemo(() => {
  if (!store.visible) return { x: 0, y: 0, width: 0 }
  const anchor = props.anchor()
  const parent = anchor.parent
  return {
    x: anchor.x - (parent?.x ?? 0),
    y: anchor.y - (parent?.y ?? 0),
    width: anchor.width,
  }
})

// Render above the anchor
<box position="absolute" top={position().y - height()} left={position().x}>
```

---

## Arrow Key Navigation Patterns

### Pattern 1: Simple Index (Hermes TUI `useMenu`)

```typescript
const [sel, setSel] = useState(0)

useInput((ch, key) => {
  if (key.upArrow && sel > 0) setSel(v => v - 1)
  if (key.downArrow && sel < rows.length - 1) setSel(v => v + 1)
  if (key.return) rows[sel]?.run()
})
```

### Pattern 2: Wrap-Around (OpenCode)

```typescript
function move(direction: -1 | 1) {
  let next = store.selected + direction
  if (next < 0) next = options().length - 1
  if (next >= options().length) next = 0
  moveTo(next)
}
```

### Pattern 3: With Scroll Tracking (OpenCode)

```typescript
function moveTo(next: number) {
  setStore("selected", next)
  if (!scroll) return
  const viewportHeight = Math.min(height(), options().length)
  const scrollBottom = scroll.scrollTop + viewportHeight
  if (next < scroll.scrollTop) {
    scroll.scrollBy(next - scroll.scrollTop)
  } else if (next + 1 > scrollBottom) {
    scroll.scrollBy(next + 1 - scrollBottom)
  }
}
```

### Pattern 4: With Page Up/Down (OpenCode DialogSelect)

```typescript
useBindings(() => ({
  commands: [
    { name: "dialog.select.prev", run() { move(-1) } },
    { name: "dialog.select.next", run() { move(1) } },
    { name: "dialog.select.page_up", run() { move(-10) } },
    { name: "dialog.select.page_down", run() { move(10) } },
    { name: "dialog.select.home", run() { moveTo(0) } },
    { name: "dialog.select.end", run() { moveTo(flat().length - 1) } },
  ],
}))
```

### Pattern 5: Mouse + Keyboard Dual Input (OpenCode)

```typescript
const [store, setStore] = createStore({
  selected: 0,
  input: "keyboard" as "keyboard" | "mouse",
})

// Mouse hover only works if last input was mouse
onMouseOver={() => {
  if (store.input !== "mouse") return
  moveTo(index)
}}

// Keyboard input resets to keyboard mode
function move(direction: -1 | 1) {
  setStore("input", "keyboard")
  // ...
}
```

---

## Scrollable Help Menus

### Hermes Classic: Paginated Help

```python
# /help command shows paginated results
CommandDef("help", "Show available commands (/help skills lists skill commands, /filters)",
         "Info", busy_policy="dispatch", execute="gateway_help", args_hint="[skills|]")
```

### Hermes TUI: Overlay with Scrollbar

```typescript
// OverlayScrollbar component
export function OverlayScrollbar({ scrollRef, t, tick }: {
  scrollRef: RefObject<null | ScrollBoxHandle>
  t: Theme
  tick: number
}) {
  // Re-renders off parent tick to handle async content resize
  void tick
  // ... renders scrollbar thumb/track
}
```

### OpenCode: DialogSelect with ScrollBox

```typescript
const height = createMemo(() => Math.min(rows(), Math.floor(dimensions().height / 2) - 6))

<scrollbox
  ref={(r: ScrollBoxRenderable) => (scroll = r)}
  maxHeight={height()}
  scrollbarOptions={{ visible: false }}
  scrollAcceleration={scrollAcceleration()}
>
  <For each={grouped()}>
    {([category, options]) => (
      <>
        <Show when={category}>
          <text fg={theme.accent} bold>{category}</text>
        </Show>
        <For each={options}>
          {(option) => (
            <box backgroundColor={active ? theme.primary : undefined}>
              <text>{option.title}</text>
            </box>
          )}
        </For>
      </>
    )}
  </For>
</scrollbox>
```

### OpenCode: Pager Overlay

```typescript
// In useInputHandlers.ts
if (overlay.pager) {
  const move = (delta: number | 'top' | 'bottom') =>
    patchOverlayState(prev => {
      const { lines, offset } = prev.pager
      const max = Math.max(0, lines.length - pagerPageSize)
      const step = delta === 'top' ? -lines.length : delta === 'bottom' ? lines.length : delta
      const next = Math.max(0, Math.min(offset + step, max))
      return next === offset ? prev : { ...prev, pager: { ...prev.pager, offset: next } }
    })

  if (key.upArrow || ch === 'k') return move(-1)
  if (key.downArrow || ch === 'j') return move(1)
  if (key.pageUp || ch === 'b') return move(-pagerPageSize)
  if (ch === 'g') return move('top')
  if (ch === 'G') return move('bottom')
  if (key.return || ch === ' ' || key.pageDown) {
    // Auto-close at last page
  }
}
```

---

## Selection vs Execution

### Hermes Classic: Separate Selection and Execution

```python
# Selection: arrow keys update cli._clarify_state["selected"]
# Execution: Enter pushes to response_queue
if key.downArrow:
    cli._clarify_state["selected"] = (cli._clarify_state["selected"] + 1) % len(choices)
if key.return:
    response_queue.put(choices[cli._clarify_state["selected"]])
```

### Hermes TUI: Immediate Execution on Enter

```typescript
if (key.return) {
  return rows[sel]?.run()  // Immediately execute
}
```

### OpenCode: Selection with Explicit Submit

```typescript
function select() {
  const selected = options()[store.selected]
  if (!selected) return
  hide()
  selected.onSelect?.()  // Execute the selected item's callback
}

// Key binding
{ name: "prompt.autocomplete.select", run() { select() } }
```

### OpenCode: Action Buttons in Footer

```typescript
// DialogSelect supports action buttons that operate on the selected item
actions={[{
  command: "dialog.delete",
  title: "Delete",
  onTrigger: (option) => deleteItem(option.value),
}]}

// Tab cycles through actions
{ key: "tab", cmd: () => moveAction(1) }
{ key: "shift+tab", cmd: () => moveAction(-1) }
```

---

## Visual Styling Patterns

### Hermes TUI: Selection Chip

```typescript
// Only active row gets background
export function listRowStyle(t: Theme, active: boolean) {
  if (!active) return {}
  const backgroundColor = t.color.completionCurrentBg
  return { backgroundColor, color: liftForContrast(t.color.text, backgroundColor, 4.5) }
}

// Cursor indicator
{active ? '▸ ' : '  '}
```

### OpenCode: Primary Color Highlight

```typescript
// Selected row uses theme.primary background
backgroundColor={index === store.selected ? theme.primary : undefined}
fg={index === store.selected ? selectedForeground(theme) : theme.text}

// Description in muted color
<text fg={index === store.selected ? selectedForeground(theme) : theme.textMuted}>
  {option().description}
</text>
```

### OpenCode: Category Headers

```typescript
<Show when={category}>
  <text fg={theme.accent} attributes={TextAttributes.BOLD}>
    {category}
  </text>
</Show>
```

### Hermes TUI: Approval Choice Labels

```typescript
_APPROVAL_CHOICE_LABELS = {
  "once": "Allow once",
  "session": "Allow for this session",
  "always": "Add to permanent allowlist",
  "deny": "Deny",
  "view": "Show full command"
}
```

---

## Key Implementation Patterns

### 1. State Machine Pattern

All three systems use a state machine for menu interaction:

```
[Idle] → (trigger key) → [Active]
[Active] → (arrow keys) → [Active] (update selection)
[Active] → (Enter) → [Executing] → [Idle]
[Active] → (Escape) → [Idle]
```

### 2. Queue-Based Blocking (Hermes Classic)

```python
# Set state, then block on queue
cli._clarify_state = { "selected": 0, "response_queue": queue.Queue() }
while True:
    try:
        result = response_queue.get(timeout=1)
        return result
    except queue.Empty:
        if timeout_expired: break
        cli._app.invalidate()  # Trigger re-render
```

### 3. Reactive Store (OpenCode)

```typescript
const [store, setStore] = createStore({
  selected: 0,
  visible: false,
  input: "keyboard",
})

// All navigation updates the store
function move(direction: -1 | 1) {
  setStore("selected", next)
  setStore("input", "keyboard")
}
```

### 4. Mode Stack (OpenCode)

```typescript
// Push mode when dialog opens
createEffect(() => {
  if (!store.visible) return
  const popMode = modeStack.push("autocomplete")
  onCleanup(popMode)
})
```

### 5. Window Offset Calculation

```typescript
// Center selected item in visible window
export const windowOffset = (count: number, selected: number, visible: number) =>
  Math.max(0, Math.min(selected - Math.floor(visible / 2), count - visible))
```

### 6. Fuzzy Scoring with Frecency

```typescript
// OpenCode combines fuzzy score with frecency
scoreFn: (objResults) => {
  let score = objResults.score
  if (objResults[0].target.startsWith(store.visible + searchValue)) score *= 2
  const frecencyScore = objResults.obj.path ? frecency.getFrecency(objResults.obj.path) : 0
  return score * (1 + frecencyScore)
}
```

### 7. Description-Aware Matching (Hermes TUI)

```typescript
// Score command name matches higher than description matches
const commandFields = [item.id, item.label, ...item.aliases]
const descriptionFields = tokenizeSearchText(item.description)
return Math.min(
  scoreFields(commandFields, query, 0),    // Offset 0
  scoreFields(descriptionFields, query, 3)  // Offset 3
)
```

---

## Comparison Summary

| Feature | Hermes Classic | Hermes TUI | OpenCode |
|---------|---------------|------------|----------|
| **Framework** | prompt_toolkit | Ink (React) | Opentui + SolidJS |
| **Menu Hook** | `useMenu()` custom | `useMenu()` | `useBindings()` |
| **Selection State** | Python variable | React state | SolidJS store |
| **Navigation** | Arrow keys | Arrow keys | Arrow keys + PageUp/Down |
| **Wrap-around** | Yes | Yes | Yes |
| **Mouse Support** | No | Yes | Yes |
| **Fuzzy Filter** | No (prefix only) | Yes (multi-tier) | Yes (fuzzysort) |
| **Frecency** | No | No | Yes |
| **Categories** | No | No | Yes |
| **Action Buttons** | No | No | Yes |
| **Ghost Text** | Yes | No | No |
| **Multi-select** | Yes (checkboxes) | No | No |
| **Mode Stack** | No | No | Yes |
| **Dialog Overlay** | Python panels | Ink overlays | Opentui dialogs |

---

## Recommendations for Implementation

1. **Start with a simple `useMenu` hook** — the Hermes TUI pattern is the cleanest starting point for arrow key navigation.

2. **Use a reactive store for selection state** — SolidJS's `createStore` or React's `useState` both work well. The key is to separate the selection index from the execution logic.

3. **Implement window offset calculation** — the `windowOffset` function is essential for scrollable lists. Center the selected item for best UX.

4. **Add fuzzy filtering early** — even a simple prefix filter is fine to start, but design the architecture so you can swap in fuzzysort later.

5. **Track mouse vs keyboard input separately** — this prevents the "mouse hover jumps selection" problem when the layout shifts.

6. **Use a mode stack for nested modals** — when a dialog opens within a dialog, the mode stack ensures the correct key bindings are active.

7. **Style only the active row** — don't paint full backgrounds on list surfaces. Use a selection chip on the active row only.

8. **Support both selection and execution keys** — Enter to select/execute, Tab to accept completion, Escape to close. Don't conflate these.

---

## Source Files Referenced

### Hermes Agent
- `hermes_cli/commands_completion.py` — SlashCommandCompleter, SlashCommandAutoSuggest
- `hermes_cli/cli_tui_mixin.py` — prompt_toolkit TUI construction
- `hermes_cli/callbacks.py` — clarify_callback, approval_callback
- `hermes_cli/cli_modal_mixin.py` — Modal overlay rendering
- `ui-tui/src/components/overlayControls.tsx` — useMenu hook, windowOffset
- `ui-tui/src/components/overlayPrimitives.tsx` — listRowStyle, chipRowProps, MenuRow
- `ui-tui/src/hooks/useCompletion.ts` — Completion request routing
- `ui-tui/src/app/useInputHandlers.ts` — Global input handling
- `ui-tui/src/app/useComposerState.ts` — Composer state management
- `ui-tui/src/domain/slash.ts` — Slash command parsing
- `ui-tui/src/app/slash/fuzzyScore.ts` — Fuzzy scoring

### OpenCode
- `packages/tui/src/component/prompt/autocomplete.tsx` — Autocomplete component
- `packages/tui/src/component/prompt/index.tsx` — Prompt component
- `packages/tui/src/ui/dialog-select.tsx` — DialogSelect component
- `packages/tui/src/ui/dialog.tsx` — Dialog overlay system
- `packages/tui/src/component/command-palette.tsx` — Command palette
- `packages/tui/src/component/dialog-skill.tsx` — Skills dialog
- `packages/tui/src/keymap.tsx` — Keymap system with mode stack
