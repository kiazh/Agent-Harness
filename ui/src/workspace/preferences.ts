// Versioned global UI preferences (closing pass): editor preferences that are
// not workspace-scoped — syntax highlighting, soft wrap, tab width — stored as
// one JSON string under the `ui_preferences` config key
// (`{"version":1,"syntax":boolean,"softWrap":boolean,"tabWidth":2|4|8}`).
//
// Security/corruption contract (mirrors workspace/persistence.ts):
// - Parsing is total: absent, corrupt, wrong-shaped or unknown-version data
//   degrades to the documented defaults and never throws; unknown keys are
//   dropped rather than copied. The document only ever holds booleans and one
//   of three numeric tab widths — never text, paths or secrets — and the
//   backend bounds the stored string (UI_PREFERENCES_MAX_BYTES).
// - Saves write exactly the whitelisted shape (values clamped), so a hostile
//   input can never round-trip through this module.

/** Outer document version for the `ui_preferences` value. */
export const UI_PREFERENCES_VERSION = 1 as const;

/** Display tab widths the UI offers and accepts. */
export const UI_TAB_WIDTHS = [2, 4, 8] as const;

export type UiTabWidth = (typeof UI_TAB_WIDTHS)[number];

export interface UiPreferences {
	syntax: boolean;
	softWrap: boolean;
	tabWidth: UiTabWidth;
}

/** Fresh defaults: highlighting on, no soft wrap, 4-column tabs. */
export function defaultUiPreferences(): UiPreferences {
	return { syntax: true, softWrap: false, tabWidth: 4 };
}

export interface ParsedUiPreferences {
	preferences: UiPreferences;
	/** True when stored data existed but was unusable (replaced by defaults). */
	migrated: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sanitizeTabWidth(value: unknown): UiTabWidth {
	return value === 2 || value === 4 || value === 8 ? value : 4;
}

/**
 * Parse one stored document. Absent input → defaults (`migrated: false`);
 * corrupt JSON, non-objects, unknown versions and out-of-range fields →
 * defaults (`migrated: true` when a stored value was replaced).
 */
export function parseUiPreferences(raw: string | undefined): ParsedUiPreferences {
	const defaults = defaultUiPreferences();
	if (typeof raw !== "string" || raw.trim() === "") return { preferences: defaults, migrated: false };
	let value: unknown;
	try {
		value = JSON.parse(raw);
	} catch {
		return { preferences: defaults, migrated: true };
	}
	if (!isRecord(value) || value.version !== UI_PREFERENCES_VERSION) {
		return { preferences: defaults, migrated: true };
	}
	const syntax = typeof value.syntax === "boolean" ? value.syntax : defaults.syntax;
	const softWrap = typeof value.softWrap === "boolean" ? value.softWrap : defaults.softWrap;
	const tabWidth = value.tabWidth === undefined ? defaults.tabWidth : sanitizeTabWidth(value.tabWidth);
	const migrated =
		(value.syntax !== undefined && typeof value.syntax !== "boolean") ||
		(value.softWrap !== undefined && typeof value.softWrap !== "boolean") ||
		(value.tabWidth !== undefined && value.tabWidth !== tabWidth);
	return { preferences: { syntax, softWrap, tabWidth }, migrated };
}

/** Serialize the exact whitelisted shape; invalid fields clamp to defaults. */
export function serializeUiPreferences(preferences: UiPreferences): string {
	return JSON.stringify({
		version: UI_PREFERENCES_VERSION,
		syntax: preferences.syntax === true,
		softWrap: preferences.softWrap === true,
		tabWidth: sanitizeTabWidth(preferences.tabWidth),
	});
}

/** The app binds this to `config.get`/`config.set("ui_preferences")`. */
export interface UiPreferencesStoreAdapter {
	get(): Promise<string | undefined>;
	set(value: string): Promise<void>;
}

/**
 * Bounded adapter over one injected string store. `load` never throws
 * (failures and corruption mean "defaults"); `save` may reject and the caller
 * decides (persistence is best-effort and never blocks editing).
 */
export class UiPreferencesPersistence {
	private readonly store: UiPreferencesStoreAdapter;

	constructor(store: UiPreferencesStoreAdapter) {
		this.store = store;
	}

	async load(): Promise<UiPreferences> {
		let raw: string | undefined;
		try {
			raw = await this.store.get();
		} catch {
			return defaultUiPreferences();
		}
		return parseUiPreferences(typeof raw === "string" ? raw : undefined).preferences;
	}

	async save(preferences: UiPreferences): Promise<void> {
		await this.store.set(serializeUiPreferences(preferences));
	}
}
