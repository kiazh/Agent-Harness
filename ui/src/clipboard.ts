import { spawn } from "node:child_process";

/**
 * Upper bound on a single clipboard copy child (pbcopy/wl-copy/xclip/Set-Clipboard).
 *
 * On timeout the child is killed and the copy reports failure, so this is the
 * only product bound between a slow-but-working backend and a user-visible
 * "Copy failed". The previous 3 s bound had ~2x headroom over the success path
 * and was breached: the clipboard transfer test measured 3.0769 s under
 * full-suite parallel load (vs ~0.78 s isolated), so a loaded machine could
 * kill a pbcopy that was still making progress. 8 s clears that worst observed
 * case by ~2.6x while staying fail-fast for a genuinely stuck backend.
 */
export const CLIPBOARD_COPY_TIMEOUT_MS = 8000;

/** Constructor options for the pi-tui fullscreen clipboard hook (no hard dep). */
export type TuiClipboardOptions = {
	copySelection: (text: string) => Promise<boolean | string>;
};

/**
 * Wire the fullscreen TUI's selection copy to the real system clipboard.
 *
 * Without this, pi-tui falls back to an OSC 52 terminal write and flashes
 * "Copied!" even on terminals that ignore OSC 52 (e.g. macOS Terminal.app) —
 * the highlight looks copied but the system clipboard never changes. With the
 * hook injected, the flash reflects the actual spawn result ("Copy failed"
 * otherwise).
 */
export function tuiClipboardOptions(): TuiClipboardOptions {
	return { copySelection: (text: string) => copyText(text) };
}

/** Copy a transient value without exposing it in shell arguments or output. */
export async function copyText(text: string): Promise<boolean> {
	const commands: Array<[string, string[]]> = process.platform === "darwin"
		? [["pbcopy", []]]
		: process.platform === "win32"
			? [["powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", "[Console]::In.ReadToEnd() | Set-Clipboard"]]]
			: [["wl-copy", []], ["xclip", ["-selection", "clipboard"]]];
	for (const [command, args] of commands) {
		const copied = await new Promise<boolean>((resolve) => {
			let settled = false;
			let timer: ReturnType<typeof setTimeout> | undefined;
			const finish = (success: boolean) => {
				if (settled) return;
				settled = true;
				clearTimeout(timer);
				resolve(success);
			};
			try {
				const child = spawn(command, args, { stdio: ["pipe", "ignore", "ignore"], windowsHide: true });
				child.once("error", () => finish(false));
				child.once("close", (code) => finish(code === 0));
				child.stdin.on("error", () => { child.kill(); finish(false); });
				timer = setTimeout(() => { child.kill(); finish(false); }, CLIPBOARD_COPY_TIMEOUT_MS);
				child.stdin.end(text, "utf8");
			} catch {
				finish(false);
			}
		});
		if (copied) return true;
	}
	return false;
}
