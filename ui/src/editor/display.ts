// Display-safe chrome text for editor/problems/dialog surfaces.
//
// The ONE sanitizer core lives in `design/text.ts`: `sanitizeLabelText` owns
// the terminal-sequence pass (OSC/APC/DCS/CSI, allow-listed SGR) plus the
// stricter single-line chrome rules. Chrome labels are single-line file names,
// language ids, dialog titles and server status words, so no control byte
// (not even a safe SGR color) or Unicode bidi/isolate control is ever
// legitimate there. This module re-exports that core under the names the
// editor sinks already use; callers must not decode a URI or interpolate
// server data straight into a rendered row.
//
// Bidi/isolate controls are invisible ordering instructions: a file named
// `\u202Egnp.exe` renders as `exe.png`, and `\u2066...\u2069` can reorder a
// path so a hostile name looks like a trusted one. They are stripped (not
// replaced) so the visible text is the honest byte sequence.

export {
	hasNoLabelControls as hasNoDisplayControls,
	sanitizeLabelText as sanitizeDisplay,
} from "../design/text.ts";
