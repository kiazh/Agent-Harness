// Single source for the keyboard-shortcut reference. The `?`/F1 picker
// (Picker rows, flat list with a value) and `/help` (context-grouped HelpKey
// rows) both render these rows, so a new binding is documented once.
//
// Order is the picker's: composer bindings, then workspace, then global; the
// help menu groups by `context` and preserves source order inside a group.

export type ShortcutContext = "Composer" | "Workspace" | "Global";

export interface ShortcutRow {
	/** Stable picker value (also the row identity in tests). */
	value: string;
	context: ShortcutContext;
	label: string;
	description?: string;
}

export const SHORTCUT_ROWS: readonly ShortcutRow[] = [
	{ value: "send", context: "Composer", label: "Enter — send", description: "Send a message or run a command" },
	{ value: "newline", context: "Composer", label: "Shift+Enter — new line" },
	{ value: "image", context: "Composer", label: "Drop an image / Ctrl+V — paste image", description: "PNG, JPEG, GIF or WebP · ⌘V where forwarded by your terminal" },
	{ value: "stop", context: "Composer", label: "Esc — stop reply", description: "Twice on empty input edits the last prompt" },
	{ value: "cancel", context: "Composer", label: "Ctrl+C — stop / clear / exit", description: "Stop a reply, clear a draft, or exit when empty" },
	{ value: "exit", context: "Composer", label: "Ctrl+D — exit", description: "When input is empty and idle" },
	{ value: "clear", context: "Composer", label: "Ctrl+L — clear chat", description: "When idle" },
	{ value: "complete", context: "Composer", label: "Tab — complete", description: "Commands and arguments" },
	{ value: "subagents", context: "Composer", label: "↓ — subagent menu", description: "On empty input: switch between the main session and its subagents" },
	{ value: "tabs", context: "Composer", label: "Ctrl+Shift+←/→ — switch agents", description: "Cycle the main session and its subagents" },
	{ value: "quickopen", context: "Workspace", label: "Ctrl+P — quick open", description: "Fuzzy file search in the workspace" },
	{ value: "save", context: "Workspace", label: "Ctrl+S — save file", description: "Save the active editor buffer" },
	{ value: "closetab", context: "Workspace", label: "Ctrl+W — close tab", description: "Close the active editor tab (dirty: save/discard/cancel); the terminal may intercept" },
	{ value: "explorer", context: "Workspace", label: "Ctrl+T — file tree", description: "Toggle the right-edge explorer; → or enter expands folders (/explorer)" },
	{ value: "intel", context: "Workspace", label: "Ctrl+Space — completion", description: "macOS may steal Ctrl+Space · fallback /editor complete · F12 definition · Shift+F12 references · F2 rename · /problems lists diagnostics" },
	{ value: "focus", context: "Workspace", label: "F6 / Shift+F6 — cycle focus", description: "Composer → editor → explorer → problems" },
	{ value: "chatmax", context: "Workspace", label: "Ctrl+→ maximize chat · Ctrl+← 50/50 split (editor focus)", description: "The composer keeps Ctrl+←/→ word motion; editor word motion stays alt+arrows" },
	{ value: "wsroot", context: "Workspace", label: "/workspace open <path> — open a folder", description: "Also /workspace split|chat|editor|ratio|side|status|close" },
	{ value: "help", context: "Global", label: "? / F1 — shortcuts", description: "? on empty input, F1 with a draft" },
	{ value: "commands", context: "Global", label: "/help — all commands", description: "This menu; /growth shows learning details" },
	{ value: "learning", context: "Global", label: "□ / ▪ — activity and learning", description: "Pulses while working and briefly when learning is saved" },
];
