// Slash-command-aware autocomplete over pi-tui's combined provider.
//
// pi-tui reports a command's *entire* argument string as the completion
// prefix (e.g. `set ` for `/keys set␣`), and its apply step replaces that
// whole prefix with the accepted item. That is correct for single-word
// arguments, but multi-stage commands (`/keys set KEY`, `/jobs …`) complete
// only the *last* token — so accepting the second suggestion deleted the
// previous word (`/keys set␣` + `OPENROUTER_API_KEY` → `/keys
// OPENROUTER_API_KEY`). This subclass narrows the argument prefix to the
// token under the cursor; every other branch (@-mentions, paths, quoted
// prefixes, bare command names) passes through untouched.
import {
	type AutocompleteSuggestions,
	CombinedAutocompleteProvider,
} from "@earendil-works/pi-tui";

export class ArgumentAwareAutocompleteProvider extends CombinedAutocompleteProvider {
	override async getSuggestions(
		lines: string[],
		cursorLine: number,
		cursorCol: number,
		options: { signal: AbortSignal; force?: boolean },
	): Promise<AutocompleteSuggestions | null> {
		const result = await super.getSuggestions(lines, cursorLine, cursorCol, options);
		if (!result) return result;
		const textBeforeCursor = (lines[cursorLine] ?? "").slice(0, cursorCol);
		const commandText = textBeforeCursor.trimStart();
		if (!commandText.startsWith("/")) return result;
		const spaceIndex = commandText.indexOf(" ");
		if (spaceIndex === -1) return result;
		// Only adjust results from the command-argument branch, identified by
		// the prefix being exactly the argument string (not a path/quote/@).
		const argumentText = commandText.slice(spaceIndex + 1);
		if (result.prefix !== argumentText) return result;
		const token = argumentText.slice(argumentText.lastIndexOf(" ") + 1);
		if (token === result.prefix) return result;
		return { items: result.items, prefix: token };
	}
}
