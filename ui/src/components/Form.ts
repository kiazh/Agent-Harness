// Durable forms: titled multiline text editing plus masked single-line
// secrets. Saves are guarded against repeated Enter/Ctrl+S while pending;
// validation errors keep the draft editable and unlock the form.

import {
	CURSOR_MARKER,
	Editor,
	type EditorOptions,
	type EditorTheme,
	getKeybindings,
	Input,
	Key,
	matchesKey,
	truncateToWidth,
	wrapTextWithAnsi,
	type Component,
	type TUI,
} from "@earendil-works/pi-tui";
import { ICONS } from "../design/icons.ts";
import { theme } from "../theme.ts";

export interface TextFormOptions {
	title: string;
	description?: string;
	initial?: string;
	placeholder?: string;
	example?: string;
	maxLength?: number;
}

/**
 * A titled multiline text form shown as an overlay (e.g. persona editing).
 * Codex menu style: borderless, docked above the composer, lowercase hints.
 * Uses the terminal Editor so Shift+Enter inserts a newline and Enter saves,
 * matching the composer. Ctrl+S also saves; Esc cancels. Errors carry a
 * text marker as well as color.
 */
export class TextForm implements Component {
	focused = false;
	readonly editor: Editor;
	onSubmit: ((value: string) => void) | undefined;
	onCancel: (() => void) | undefined;

	private readonly title: string;
	private readonly description?: string;
	private readonly placeholder?: string;
	private readonly example?: string;
	private readonly maxLength?: number;
	private readonly tui: TUI;
	private error = "";
	private saveLocked = false;

	constructor(tui: TUI, editorTheme: EditorTheme, options: TextFormOptions) {
		this.tui = tui;
		this.title = options.title;
		this.description = options.description;
		this.placeholder = options.placeholder;
		this.example = options.example;
		this.maxLength = options.maxLength;
		this.editor = new Editor(tui, editorTheme, { paddingX: 1 } satisfies EditorOptions);
		this.editor.setText(options.initial ?? "");
		this.editor.onSubmit = (value) => this.save(value);
		this.editor.onChange = () => {
			if (this.error) this.error = "";
			this.tui.requestRender();
		};
	}

	setError(message: string): void {
		this.error = message;
		this.saveLocked = false;
		this.tui.requestRender();
	}

	getValue(): string {
		return this.editor.getExpandedText();
	}

	/** Guarded save: repeated Enter/Ctrl+S while pending submits exactly once. */
	private save(value: string): void {
		if (this.saveLocked) return;
		this.saveLocked = true;
		this.onSubmit?.(value);
	}

	handleInput(data: string): void {
		const kb = getKeybindings();
		if (kb.matches(data, "tui.select.cancel")) {
			this.onCancel?.();
			return;
		}
		// Explicit save shortcut alongside Enter (the Editor submits on Enter).
		if (data === "\x13") {
			this.save(this.getValue());
			return;
		}
		// While a save is pending the Editor would submit-and-clear on Enter;
		// swallow it so the draft survives for the error path.
		if (this.saveLocked && (matchesKey(data, Key.enter) || data === "\r" || data === "\n")) return;
		this.editor.focused = true;
		this.editor.handleInput(data);
	}

	render(width: number): string[] {
		const content = Math.max(1, width);
		// Match App's 70% sheet capped by its five-row bottom margin. Optional
		// guidance yields space to the cursor, count, errors, and controls.
		const rows = this.tui.terminal.rows;
		const height = Math.max(1, Math.min(Math.floor(rows * 70 / 100), rows - 5));
		const wrap = (text: string): string[] => text.split("\n").flatMap((line) => wrapTextWithAnsi(line, content));
		const value = this.getValue();
		// Reserve one editable row first, then controls and count. On the
		// shortest screens the title and editor borders yield to these tasks.
		const statusRows = Math.min(2, height - 1);
		const errorBudget = Math.max(0, Math.min(Math.max(1, height - 6), height - statusRows - 1));
		const errors = this.error ? wrap(this.error).slice(0, errorBudget) : [];
		const guidance = [
			...(this.description ? wrap(this.description) : []),
			...(!value && this.placeholder ? wrap(this.placeholder) : []),
			...(this.example ? wrap(`Example: ${this.example}`) : []),
		].slice(0, Math.max(0, height - 6 - errors.length));
		const out = [
			...(height - statusRows - errors.length >= 2 ? [truncateToWidth(theme.dim(this.title), content)] : []),
			...guidance.map(theme.dim),
		];
		this.editor.focused = this.focused;
		const editorRows = this.editor.render(content);
		const editorHeight = Math.max(1, height - out.length - errors.length - statusRows);
		if (editorRows.length > editorHeight) {
			// pi-tui's Editor has a minimum of five text rows; retain a window
			// around its cursor instead of letting the overlay clip its tail.
			const textRows = editorRows.slice(1, -1);
			const showBorders = editorHeight >= 3;
			const visible = editorHeight - (showBorders ? 2 : 0);
			const cursor = textRows.findIndex((line) => line.includes(CURSOR_MARKER) || line.includes("\x1b[7m"));
			const start = Math.max(0, Math.min(textRows.length - visible, cursor - visible + 1));
			if (showBorders) out.push(start > 0 ? theme.dim("↑ more") : editorRows[0]!);
			out.push(...textRows.slice(start, start + visible));
			if (showBorders) out.push(start + visible < textRows.length ? theme.dim("↓ more") : editorRows.at(-1)!);
		} else {
			out.push(...editorRows);
		}
		const count = Array.from(value).length;
		const countText = this.maxLength ? `${count} / ${this.maxLength} characters` : `${count} characters`;
		if (statusRows >= 2) out.push(truncateToWidth(theme.dim(countText), content));
		out.push(...errors.map((line) => truncateToWidth(theme.error(`✗ ${line}`), content)));
		const hints = content >= 56 ? "enter save · shift+enter newline · ctrl+s save · esc cancel" : "enter save · ctrl+s save · esc cancel";
		if (statusRows >= 1) out.push(truncateToWidth(theme.dim(hints), content));
		return out;
	}

	invalidate(): void {
		this.editor.invalidate();
	}
}

export interface SecretFormOptions {
	title: string;
	description?: string;
	placeholder?: string;
}

/**
 * Masked single-line secret editor. The raw value is never rendered: every
 * frame paints one mask dot per UTF-16 unit (keeping the inherited cursor,
 * paste, undo and navigation math exact). Blank values are rejected in the
 * form; Esc cancels. References are dropped after submit/cancel as hygiene
 * (no guaranteed memory erasure is claimed).
 */
export class SecretInput extends Input {
	override render(width: number): string[] {
		const inner = this as unknown as { value: string };
		const real = inner.value;
		if (!real) return super.render(width);
		inner.value = ICONS.secretMask.repeat(real.length);
		try {
			return super.render(width);
		} finally {
			inner.value = real;
		}
	}

	/** Drop the retained reference (undo history may still hold copies). */
	clearSecret(): void {
		this.setValue("");
	}
}

export class SecretForm implements Component {
	focused = false;
	readonly input: SecretInput;
	onSubmit: ((value: string) => void) | undefined;
	onCancel: (() => void) | undefined;

	private readonly title: string;
	private readonly description?: string;
	private readonly tui: TUI;
	private error = "";
	private saveLocked = false;

	constructor(tui: TUI, options: SecretFormOptions) {
		this.tui = tui;
		this.title = options.title;
		this.description = options.description;
		this.input = new SecretInput({ prompt: "> ", placeholder: options.placeholder ?? "" });
		this.input.onSubmit = (value) => this.attempt(value);
		this.input.onEscape = () => this.cancel();
	}

	setError(message: string): void {
		this.error = message;
		this.saveLocked = false;
		this.tui.requestRender();
	}

	getValue(): string {
		return this.input.getValue();
	}

	private attempt(value: string): void {
		if (this.saveLocked) return;
		if (!value.trim()) {
			this.setError("Value is required.");
			return;
		}
		this.saveLocked = true;
		this.input.clearSecret();
		this.onSubmit?.(value);
	}

	private cancel(): void {
		this.input.clearSecret();
		this.onCancel?.();
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.escape)) {
			this.cancel();
			return;
		}
		if (data === "\x13") {
			this.attempt(this.getValue());
			return;
		}
		this.input.focused = true;
		this.input.handleInput(data);
	}

	render(width: number): string[] {
		const content = Math.max(1, width);
		const out = [truncateToWidth(theme.dim(this.title), content)];
		if (this.description) out.push(truncateToWidth(theme.dim(this.description), content));
		this.input.focused = this.focused;
		out.push(...this.input.render(content));
		if (this.error) out.push(truncateToWidth(theme.error(`✗ ${this.error}`), content));
		out.push(truncateToWidth(theme.dim("enter save · ctrl+s save · esc cancel"), content));
		return out;
	}

	invalidate(): void {}
}
