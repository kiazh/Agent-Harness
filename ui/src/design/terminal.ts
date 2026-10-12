// Paint the app's alternate buffer using SGR backgrounds, never OSC palette
// mutations. Empty rows and erase-to-end operations inherit the same colour.
import { backgroundAnsi, getTerminalColorMode, parseColor, ProcessTerminal } from "@earendil-works/pi-tui";
import { getSkin, semanticPalette } from "./theme.ts";

const ENTER = "\x1b[?1049h";
const LEAVE = "\x1b[?1049l";
const RESET = "\x1b[0m";
// eslint-disable-next-line no-control-regex
const SGR = /\x1b\[([\d;]*)m/g;

export class ThemeBackground {
	private active = false;

	paint(data: string, columns = 0): string {
		const enter = data.indexOf(ENTER);
		if (!this.active) {
			if (enter < 0) return data;
			this.active = true;
			return data.slice(0, enter + ENTER.length) + this.paint(data.slice(enter + ENTER.length), columns);
		}
		const leave = data.indexOf(LEAVE);
		const body = leave < 0 ? data : data.slice(0, leave);
		const bg = backgroundAnsi(parseColor(semanticPalette(getSkin())!.background), getTerminalColorMode());
		// Preserve panel/selection fills; restore the base only when a style
		// explicitly resets all attributes or the background attribute.
		const styled = body.replace(SGR, (sequence, params: string) => {
			const codes = params.split(";").map(Number);
			let restore = false;
			for (let i = 0; i < codes.length; i++) {
				const code = codes[i];
				if (code === 0 || code === 49) restore = true;
				if (code === 48 || (code !== undefined && code >= 40 && code <= 47) || (code !== undefined && code >= 100 && code <= 107)) restore = false;
				// Zero-valued RGB channels and palette indices are colours,
				// not reset codes. Skip extended-colour arguments as a unit.
				if (code === 38 || code === 48 || code === 58) {
					if (codes[i + 1] === 2) i += 4;
					else if (codes[i + 1] === 5) i += 2;
				}
			}
			return restore ? sequence + bg : sequence;
		});
		// Apple Terminal and other non-BCE terminals erase to their default
		// colour rather than the active SGR background. Paint real cells;
		// save/restore the cursor so subsequent content starts in its place.
		const fill = Number.isFinite(columns) && columns > 0
			? `${bg}\x1b[2K\x1b7\r${" ".repeat(Math.floor(columns))}\x1b8`
			: "\x1b[2K";
		const painted = bg + styled.split("\x1b[2K").join(fill) + RESET;
		if (leave < 0) return painted;
		this.active = false;
		return painted + data.slice(leave);
	}
}

/** Real terminal path; renderers, overlays and blank rows share one fill. */
export class ThemedTerminal extends ProcessTerminal {
	private readonly background = new ThemeBackground();

	override write(data: string): void {
		super.write(this.background.paint(data, this.columns));
	}
}
