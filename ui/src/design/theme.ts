// Machina skins: light and dark palettes, including Codex-derived skins,
// plus house skins. Foreground tokens (text through info) follow Codex's
// values; card backgrounds and borders are derived from each theme's
// background + ink with mixColors, the way Codex derives subtle surface
// elevations. The composer fill stays a neutral lift (Codex
// backgroundElement); selection rows paint accent bg with a derived,
// contrast-checked ink token (>= 4.5:1; see selectionInk). Every skin's `dim`
// token is contrast-checked to >= 4.5:1 against its own background while
// staying below that skin's `muted`, because `dim` carries essential footer/
// menu/tool-status text (review P3-2).
//
// Style functions resolve the *current* skin lazily, so `setSkin()` recolors
// the whole UI without a restart. The choice persists via `config.theme`.

import {
	type Color,
	type EditorTheme,
	type MarkdownTheme,
	type SelectListTheme,
	backgroundAnsi,
	colorToHex,
	colorToRgb,
	foregroundAnsi,
	getTerminalColorMode,
	mixColors,
	parseColor,
	rgbColor,
	styleTextWithAnsi,
	type TextAttributes,
	visibleWidth,
} from "@earendil-works/pi-tui";
import { CODEX_SEMANTIC_START, type SemanticPalette } from "./tokens.ts";

export type { SemanticPalette };

export type PaletteName =
	| "text"
	| "muted"
	| "dim"
	| "accent"
	| "secondary"
	| "success"
	| "warning"
	| "error"
	| "info"
	| "border"
	| "userBg"
	| "assistantBg"
	| "toolBg"
	| "toolOkBg"
	| "toolErrBg";

type BaseSlot =
	| "text"
	| "muted"
	| "dim"
	| "accent"
	| "secondary"
	| "success"
	| "warning"
	| "error"
	| "info"
	| "neutral";

interface SkinDef {
	description: string;
	art: string[];
	base: Record<BaseSlot, string>;
}

const SKINS: Record<string, SkinDef> = {
	default: {
		description: "opencode theme (dark) — the house default",
		art: ["  ◆──────◆", " │ AGENT  │", " │ HARNESS│", "  ◆──────◆"],
		base: {
			text: "#eeeeee",
			muted: "#808080",
			dim: "#7c7c7c",
			accent: "#fab283",
			secondary: "#9d7cd8",
			success: "#7fd88f",
			warning: "#f5a742",
			error: "#e06c75",
			info: "#56b6c2",
			neutral: "#0a0a0a",
		},
	},
	tokyonight: {
		description: "Tokyonight (dark)",
		art: ["   .--.  ", "  ( moon)", "   `--'  ", "  *  . * "],
		base: {
			text: "#c0caf5",
			muted: "#8389ac",
			dim: "#7982ab",
			accent: "#7aa2f7",
			secondary: "#bb9af7",
			success: "#9ece6a",
			warning: "#e0af68",
			error: "#f7768e",
			info: "#7dcfff",
			neutral: "#1a1b26",
		},
	},
	catppuccin: {
		description: "Catppuccin Mocha (dark)",
		art: ["  (=^.^=)", "  │mocha│", "  │latte│", "  └─────┘"],
		base: {
			text: "#cdd6f4",
			muted: "#888da3",
			dim: "#82869d",
			accent: "#b4befe",
			secondary: "#f38ba8",
			success: "#a6d189",
			warning: "#f9e2af",
			error: "#e78284",
			info: "#89dceb",
			neutral: "#1e1e2e",
		},
	},
	dracula: {
		description: "Dracula (dark)",
		art: ["  ◇────◇", "  │DRAC │", "  │ ULA │", "  ◇────◇"],
		base: {
			text: "#f8f8f2",
			muted: "#a8abbf",
			dim: "#8491b8",
			accent: "#bd93f9",
			secondary: "#ff79c6",
			success: "#50fa7b",
			warning: "#ffb86c",
			error: "#ff5555",
			info: "#8be9fd",
			neutral: "#282a36",
		},
	},
	gruvbox: {
		description: "Gruvbox (dark)",
		art: ["  ┌─────┐", "  │GRUV │", "  │ BOX │", "  └─────┘"],
		base: {
			text: "#ebdbb2",
			muted: "#a89a85",
			dim: "#9a8d7f",
			accent: "#83a598",
			secondary: "#d3869b",
			success: "#b8bb26",
			warning: "#fabd2f",
			error: "#fb4934",
			info: "#8ec07c",
			neutral: "#282828",
		},
	},
	rosepine: {
		description: "Rosé Pine (dark)",
		art: ["  ♡────♡", "  │ROSE │", "  │ PINE│", "  ♡────♡"],
		base: {
			text: "#e0def4",
			muted: "#908caa",
			dim: "#827f99",
			accent: "#c4a7e7",
			secondary: "#ebbcba",
			success: "#9ccfd8",
			warning: "#f6c177",
			error: "#eb6f92",
			info: "#56949f",
			neutral: "#191724",
		},
	},
	nord: {
		description: "Nord (dark)",
		art: ["  ▲────▲", "  │NORD │", "  │FROST│", "  ▲────▲"],
		base: {
			text: "#e5e9f0",
			muted: "#9aa4b7",
			dim: "#929db1",
			accent: "#88c0d0",
			secondary: "#d57780",
			success: "#a3be8c",
			warning: "#ebcb8b",
			error: "#bf616a",
			info: "#81a1c1",
			neutral: "#2e3440",
		},
	},
	everforest: {
		description: "Everforest (dark)",
		art: ["    ▲    ", "   ▲▲▲   ", "  ▲▲▲▲▲  ", "    │    "],
		base: {
			text: "#d3c6aa",
			muted: "#9ea69d",
			dim: "#979f96",
			accent: "#7fbbb3",
			secondary: "#d699b6",
			success: "#a7c080",
			warning: "#dbbc7f",
			error: "#e67e80",
			info: "#83c092",
			neutral: "#2d353b",
		},
	},
	matrix: {
		description: "Matrix (dark)",
		art: ["  0101010", "  MATRIX ", "  1010101", "  HACKER "],
		base: {
			text: "#62ff94",
			muted: "#8ca391",
			dim: "#69806f",
			accent: "#2eff6a",
			secondary: "#c770ff",
			success: "#62ff94",
			warning: "#e6ff57",
			error: "#ff5555",
			info: "#30b3ff",
			neutral: "#0a0e0a",
		},
	},
	codex: {
		description: "Codex-style quiet chrome — pale blue accents and neutral surfaces",
		art: ["  ┌─────┐", "  │ >_  │", "  │ AH  │", "  └─────┘"],
		base: {
			text: CODEX_SEMANTIC_START.text,
			muted: CODEX_SEMANTIC_START.muted,
			// AA lift of CODEX_SEMANTIC_START.faint (#5d5d66 = 3.02:1 on
			// #0b0b0c): the startup skin's `dim` must clear 4.5:1 (see P3-2).
			dim: "#797984",
			accent: CODEX_SEMANTIC_START.accent,
			secondary: CODEX_SEMANTIC_START.accentSoft,
			success: CODEX_SEMANTIC_START.success,
			warning: CODEX_SEMANTIC_START.warning,
			error: CODEX_SEMANTIC_START.danger,
			info: "#7dcfff",
			neutral: CODEX_SEMANTIC_START.background,
		},
	},
	"catppuccin-frappe": {
		description: "Catppuccin Frappé (dark)",
		art: ["  ƒ────ƒ", "  │FRAP │", "  │PUCC │", "  ƒ────ƒ"],
		base: {
			text: "#c6d0f5",
			muted: "#b5bfe2",
			dim: "#949cb8",
			accent: "#8da4e2",
			secondary: "#f4b8e4",
			success: "#a6d189",
			warning: "#e5c890",
			error: "#e78284",
			info: "#81c8be",
			neutral: "#303446",
		},
	},
	"catppuccin-macchiato": {
		description: "Catppuccin Macchiato (dark)",
		art: ["  µ────µ", "  │MACC │", "  │HIATO│", "  µ────µ"],
		base: {
			text: "#cad3f5",
			muted: "#b8c0e0",
			dim: "#939ab7",
			accent: "#8aadf4",
			secondary: "#f5bde6",
			success: "#a6da95",
			warning: "#eed49f",
			error: "#ed8796",
			info: "#8bd5ca",
			neutral: "#24273a",
		},
	},
	cobalt2: {
		description: "Cobalt2 (dark)",
		art: ["  ◆────◆", "  │COBA │", "  │ LT2 │", "  ◆────◆"],
		base: {
			text: "#ffffff",
			muted: "#adb7c9",
			dim: "#6aa1c8",
			accent: "#0088ff",
			secondary: "#2affdf",
			success: "#9eff80",
			warning: "#ffc600",
			error: "#ff0088",
			info: "#ff9d00",
			neutral: "#193549",
		},
	},
	ayu: {
		description: "Ayu Dark",
		art: ["  ○────○", "  │AYU  │", "  │DARK │", "  ○────○"],
		base: {
			text: "#d6dae0",
			muted: "#8f96a3",
			dim: "#718090",
			accent: "#3fb7e3",
			secondary: "#f2856f",
			success: "#78d05c",
			warning: "#e4a75c",
			error: "#f58572",
			info: "#66c6f1",
			neutral: "#0f1419",
		},
	},
	nightowl: {
		description: "Night Owl (dark)",
		art: ["  ◑────◑", "  │NIGHT│", "  │ OWL │", "  ◑────◑"],
		base: {
			text: "#d6deeb",
			muted: "#8fa0ae",
			dim: "#6d8383",
			accent: "#82aaff",
			secondary: "#c792ea",
			success: "#c5e478",
			warning: "#ecc48d",
			error: "#ef5350",
			info: "#7fdbca",
			neutral: "#011627",
		},
	},
	"one-dark": {
		description: "One Dark",
		art: ["  ◇────◇", "  │ONE  │", "  │DARK │", "  ◇────◇"],
		base: {
			text: "#abb2bf",
			muted: "#959ba6",
			dim: "#8c94a1",
			accent: "#61afef",
			secondary: "#c678dd",
			success: "#98c379",
			warning: "#e5c07b",
			error: "#e06c75",
			info: "#d19a66",
			neutral: "#282c34",
		},
	},
	flexoki: {
		description: "Flexoki Dark",
		art: ["  ≈────≈", "  │FLEX │", "  │ OKI │", "  ≈────≈"],
		base: {
			text: "#cecdc3",
			muted: "#84827c",
			dim: "#7d7c76",
			accent: "#da702c",
			secondary: "#8b7ec8",
			success: "#879a39",
			warning: "#d0a215",
			error: "#d14d41",
			info: "#3aa99f",
			neutral: "#100f0f",
		},
	},
	mono: {
		description: "High-contrast greys, no hue (accessible) — house skin",
		art: ["  ┌───┐  ", "  │ A │  ", "  │ H │  ", "  └───┘  "],
		base: {
			text: "#e8e8e8",
			muted: "#a0a0a0",
			dim: "#808080",
			accent: "#ffffff",
			secondary: "#c0c0c0",
			success: "#d0d0d0",
			warning: "#b8b8b8",
			error: "#909090",
			info: "#c8c8c8",
			neutral: "#101010",
		},
	},
	onedark: {
		description: "One Dark (dark)",
		art: ["  ○────○", "  │ONE  │", "  │DARK │", "  ○────○"],
		base: {
			text: "#abb2bf",
			muted: "#949ba7",
			dim: "#8c94a1",
			accent: "#61afef",
			secondary: "#c678dd",
			success: "#98c379",
			warning: "#e5c07b",
			error: "#e06c75",
			info: "#56b6c2",
			neutral: "#282c34",
		},
	},
	monokai: {
		description: "Monokai (dark)",
		art: ["  □────□", "  │MONO │", "  │ KAI │", "  □────□"],
		base: {
			text: "#f8f8f2",
			muted: "#9a9178",
			dim: "#938f79",
			accent: "#66d9ef",
			secondary: "#ae81ff",
			success: "#a6e22e",
			warning: "#e6db74",
			error: "#f92672",
			info: "#fd971f",
			neutral: "#272822",
		},
	},
	github: {
		description: "GitHub Dark",
		art: ["  ●────●", "  │HUB  │", "  │ GIT │", "  ●────●"],
		base: {
			text: "#e6edf3",
			muted: "#7d8590",
			dim: "#757d89",
			accent: "#2f81f7",
			secondary: "#ab7df8",
			success: "#3fb950",
			warning: "#d29922",
			error: "#f85149",
			info: "#39c5cf",
			neutral: "#0d1117",
		},
	},
	solarized: {
		description: "Solarized Dark",
		art: ["  ◐────◐", "  │SOLAR│", "  │IZED │", "  ◐────◐"],
		base: {
			text: "#93a1a1",
			muted: "#839496",
			dim: "#78929a",
			accent: "#268bd2",
			secondary: "#d33682",
			success: "#859900",
			warning: "#b58900",
			error: "#dc322f",
			info: "#2aa198",
			neutral: "#002b36",
		},
	},
	kanagawa: {
		description: "Kanagawa Wave (dark)",
		art: ["  ≈────≈", "  │KANA │", "  │GAWA │", "  ≈────≈"],
		base: {
			text: "#dcd7ba",
			muted: "#888ea0",
			dim: "#828796",
			accent: "#7e9cd8",
			secondary: "#957fb8",
			success: "#98bb6c",
			warning: "#e6c384",
			error: "#e46876",
			info: "#7fb4ca",
			neutral: "#1f1f28",
		},
	},
	palenight: {
		description: "Palenight (dark)",
		art: ["  ◑────◑", "  │PALE │", "  │NIGHT│", "  ◑────◑"],
		base: {
			text: "#a6accd",
			muted: "#969bb7",
			dim: "#8e94b4",
			accent: "#82aaff",
			secondary: "#c792ea",
			success: "#c3e88d",
			warning: "#ffcb6b",
			error: "#f07178",
			info: "#89ddff",
			neutral: "#292d3e",
		},
	},
	opencode: {
		description: "Opencode (dark) — calm blue/indigo accent",
		art: ["  ┌─────┐", "  │OPEN │", "  │CODE │", "  └─────┘"],
		base: {
			text: "#e4e4e7",
			muted: "#8b8b94",
			dim: "#797984",
			accent: "#7aa2f7",
			secondary: "#9d7cd8",
			success: "#9ece6a",
			warning: "#e0af68",
			error: "#f7768e",
			info: "#7dcfff",
			neutral: "#0b0b0c",
		},
	},
	light: {
		description: "Light — dark text on neutral light surfaces",
		art: ["  ┌─────┐", "  │LIGH │", "  │  T  │", "  └─────┘"],
		base: {
			text: "#1a1a1e",
			muted: "#565660",
			dim: "#6e6e79",
			accent: "#2f5fd0",
			secondary: "#6a4fc7",
			success: "#1a7f37",
			warning: "#9a6700",
			error: "#cf222e",
			info: "#1a7f8a",
			neutral: "#f4f4f5",
		},
	},
	linen: {
		description: "Warm paper · terracotta and violet accents (light)",
		art: ["  ┌─────┐", "  │LINEN│", "  │  AH │", "  └─────┘"],
		base: {
			text: "#302b2a",
			muted: "#625a57",
			dim: "#716c69",
			accent: "#88513e",
			secondary: "#5956a6",
			success: "#2f6d4d",
			warning: "#8a5b13",
			error: "#aa3540",
			info: "#27677b",
			neutral: "#f7f1e7",
		},
	},
	lagoon: {
		description: "Sea glass · teal and lilac accents (light)",
		art: ["  ┌─────┐", "  │LAG  │", "  │ OON │", "  └─────┘"],
		base: {
			text: "#18353a",
			muted: "#44656a",
			dim: "#5d7073",
			accent: "#236a7b",
			secondary: "#7054aa",
			success: "#276c55",
			warning: "#8a5f22",
			error: "#a7424a",
			info: "#316386",
			neutral: "#eaf4f2",
		},
	},
	afterglow: {
		description: "Twilight ink · lavender and peach accents (dark)",
		art: ["  ┌─────┐", "  │AFTER│", "  │ GLOW│", "  └─────┘"],
		base: {
			text: "#eeeaf7",
			muted: "#adb4cc",
			dim: "#828aa9",
			accent: "#d9b3f5",
			secondary: "#f4aeac",
			success: "#9fd6c5",
			warning: "#e9c783",
			error: "#f08d9f",
			info: "#8fcce2",
			neutral: "#191b2d",
		},
	},
	aurora: {
		description: "Aurora borealis · green and violet over midnight blue (dark)",
		art: ["  ~─~─~─~", "  │AURORA│", "  │ BORE │", "  ~─~─~─~"],
		base: {
			text: "#e7f0ff",
			muted: "#93a6c4",
			dim: "#8093b3",
			accent: "#6fe3c4",
			secondary: "#b78cf0",
			success: "#7ee5a4",
			warning: "#e9c07a",
			error: "#ff6f8b",
			info: "#7fbcff",
			neutral: "#0b1220",
		},
	},
	ember: {
		description: "Ember · warm orange glow on charred brown (dark)",
		art: ["  ▲─────▲", "  │EMBER │", "  │ FIRE │", "  ▲─────▲"],
		base: {
			text: "#f6e7d8",
			muted: "#bda089",
			dim: "#a88c74",
			accent: "#ff8c42",
			secondary: "#d98cb3",
			success: "#9ecb6b",
			warning: "#f2c14e",
			error: "#f0553d",
			info: "#6fb3c9",
			neutral: "#1a0f0a",
		},
	},
	forest: {
		description: "Forest canopy · mossy greens and violet (dark)",
		art: ["   ▲▲▲  ", "  ▲▲▲▲▲ ", "  │TREE │", "  └─────┘"],
		base: {
			text: "#d8ead2",
			muted: "#93b39c",
			dim: "#83a58d",
			accent: "#6fcf97",
			secondary: "#b58bd1",
			success: "#7ddc8a",
			warning: "#e0b15c",
			error: "#e8756b",
			info: "#6fd3c7",
			neutral: "#0e1a12",
		},
	},
	glacier: {
		description: "Glacier melt · cool blue on ice white (light)",
		art: ["  ◇─────◇", "  │GLACIER", "  │  ICY │", "  ◇─────◇"],
		base: {
			text: "#12303f",
			muted: "#4a6474",
			dim: "#5b6f7c",
			accent: "#1f6f9e",
			secondary: "#6a5acd",
			success: "#1f7a4d",
			warning: "#8a5a12",
			error: "#b03a4a",
			info: "#206f86",
			neutral: "#eef4fa",
		},
	},
	"neon-grid": {
		description: "Neon grid · synthwave magenta and cyan (dark)",
		art: ["  ▦▦▦▦▦▦▦", "  │NEON  │", "  │ GRID │", "  ▦▦▦▦▦▦▦"],
		base: {
			text: "#f2e9ff",
			muted: "#b39ddb",
			dim: "#8f7ec4",
			accent: "#ff3df0",
			secondary: "#00e5ff",
			success: "#39ff9e",
			warning: "#ffe14d",
			error: "#ff4d6d",
			info: "#4dc3ff",
			neutral: "#0a0018",
		},
	},
	paper: {
		description: "Ink on paper · muted warm neutrals (light)",
		art: ["  ┌─────┐", "  │PAPER│", "  │     │", "  └─────┘"],
		base: {
			text: "#2b2a27",
			muted: "#5f5c55",
			dim: "#6e6b64",
			accent: "#5b6b8c",
			secondary: "#7a5e8c",
			success: "#4a7a4a",
			warning: "#8a6a1e",
			error: "#a6444a",
			info: "#3a6b78",
			neutral: "#f6f3ec",
		},
	},
	"sepia-crt": {
		description: "Sepia CRT · amber phosphor on aged paper (light)",
		art: ["  ┌─────┐", "  │SEPIA│", "  │ CRT │", "  └─────┘"],
		base: {
			text: "#3a2f1e",
			muted: "#6f5f42",
			dim: "#746343",
			accent: "#8f5018",
			secondary: "#7a4b8a",
			success: "#4f6b2e",
			warning: "#7d560d",
			error: "#a83a2f",
			info: "#35646a",
			neutral: "#f0e2c8",
		},
	},
	blueprint: {
		description: "Blueprint · cyan linework on drafting navy (dark)",
		art: ["  ╱─────╲", "  │BLUE  │", "  │PRINT │", "  ╲─────╱"],
		base: {
			text: "#dbeafe",
			muted: "#8fb0d8",
			dim: "#7fa0cc",
			accent: "#7fd4ff",
			secondary: "#ffd27f",
			success: "#7fe0a0",
			warning: "#ffcc66",
			error: "#ff8080",
			info: "#9fc6ff",
			neutral: "#0a1e3f",
		},
	},
	"rose-quartz": {
		description: "Rose quartz · warm pink and plum (light)",
		art: ["  ❋─────❋", "  │ROSE  │", "  │QUARTZ│", "  ❋─────❋"],
		base: {
			text: "#3a2530",
			muted: "#7a5b66",
			dim: "#83646f",
			accent: "#c2185b",
			secondary: "#7b5ea7",
			success: "#35704a",
			warning: "#7d560d",
			error: "#c0392b",
			info: "#276a78",
			neutral: "#fbf0f3",
		},
	},
	"high-contrast-dark": {
		description: "High contrast · pure white on black (accessibility, dark)",
		art: ["  ███████", "  │ HI   │", "  │ CONT │", "  ███████"],
		base: {
			text: "#ffffff",
			muted: "#c8c8c8",
			dim: "#a0a0a0",
			accent: "#ffd400",
			secondary: "#00e0ff",
			success: "#00e676",
			warning: "#ffab00",
			error: "#ff5252",
			info: "#40c4ff",
			neutral: "#000000",
		},
	},
	"high-contrast-light": {
		description: "High contrast · black on white (accessibility, light)",
		art: ["  ░░░░░░░", "  │ HI   │", "  │ CONT │", "  ░░░░░░░"],
		base: {
			text: "#000000",
			muted: "#4a4a4a",
			dim: "#5c5c5c",
			accent: "#0b3d91",
			secondary: "#6a1b9a",
			success: "#0b6b2f",
			warning: "#8a4b00",
			error: "#c62828",
			info: "#0b6b8a",
			neutral: "#ffffff",
		},
	},
	"sunset-drive": {
		description: "Sunset drive · coral and gold over deep plum (dark)",
		art: ["  ◐─────◐", "  │SUNSET│", "  │ DRIVE│", "  ◐─────◐"],
		base: {
			text: "#ffe8d6",
			muted: "#c9a0c0",
			dim: "#a87ba0",
			accent: "#ff6b6b",
			secondary: "#ffb86c",
			success: "#7ee081",
			warning: "#ffd166",
			error: "#ff5c8a",
			info: "#7ec8e3",
			neutral: "#1a0b2e",
		},
	},
	"matcha": {
		description: "Matcha · forest ink, moss selections and plum details (light)",
		art: ["  ┌──────┐", "  │ MATCHA│", "  │      │", "  └──────┘"],
		base: {
			neutral: "#f0f3e5",
			text: "#26382b",
			muted: "#50604e",
			dim: "#596951",
			accent: "#426b35",
			secondary: "#785373",
			success: "#326545",
			warning: "#865709",
			error: "#ac3544",
			info: "#286577",
		},
	},
	"porcelain": {
		description: "Porcelain · cobalt actions on crisp ivory, copper details (light)",
		art: ["  ┌──────┐", "  │ PORCEL│", "  │      │", "  └──────┘"],
		base: {
			neutral: "#faf8f2",
			text: "#232b38",
			muted: "#535d6a",
			dim: "#626976",
			accent: "#254ea1",
			secondary: "#945039",
			success: "#256542",
			warning: "#875600",
			error: "#ac3042",
			info: "#26667b",
		},
	},
	"sakura": {
		description: "Sakura · cherry ink, mulberry selections and jade details (light)",
		art: ["  ┌──────┐", "  │ SAKURA│", "  │      │", "  └──────┘"],
		base: {
			neutral: "#fff1f2",
			text: "#3e2634",
			muted: "#72515e",
			dim: "#7c5a67",
			accent: "#9a315b",
			secondary: "#535d97",
			success: "#28684c",
			warning: "#875511",
			error: "#af303b",
			info: "#286b7b",
		},
	},
	"sandstone": {
		description: "Sandstone · burnt sienna, mineral blue and warm parchment (light)",
		art: ["  ┌──────┐", "  │ SANDST│", "  │      │", "  └──────┘"],
		base: {
			neutral: "#f8eddb",
			text: "#392c24",
			muted: "#685448",
			dim: "#735f51",
			accent: "#964221",
			secondary: "#3d627f",
			success: "#3c653b",
			warning: "#82540d",
			error: "#af3430",
			info: "#2c6474",
		},
	},
	"tidal": {
		description: "Tidal · petrol ink, deep teal actions and coral details (light)",
		art: ["  ┌──────┐", "  │ TIDAL │", "  │      │", "  └──────┘"],
		base: {
			neutral: "#edf7f5",
			text: "#193c3b",
			muted: "#426660",
			dim: "#52716b",
			accent: "#12665e",
			secondary: "#9b4757",
			success: "#2c6948",
			warning: "#825811",
			error: "#ad3745",
			info: "#285f91",
		},
	},
	"lilac": {
		description: "Lilac · violet ink and iris selections on pale lavender (light)",
		art: ["  ┌──────┐", "  │ LILAC │", "  │      │", "  └──────┘"],
		base: {
			neutral: "#f5f1ff",
			text: "#332b49",
			muted: "#615774",
			dim: "#6c627d",
			accent: "#65419a",
			secondary: "#8d485d",
			success: "#2b6b53",
			warning: "#855511",
			error: "#b1344a",
			info: "#306482",
		},
	},
	"marigold": {
		description: "Marigold · golden paper, umber actions and indigo details (light)",
		art: ["  ┌──────┐", "  │ MARIGO│", "  │      │", "  └──────┘"],
		base: {
			neutral: "#fff4d6",
			text: "#3c321b",
			muted: "#695b3c",
			dim: "#746343",
			accent: "#825408",
			secondary: "#4e5492",
			success: "#426632",
			warning: "#80500a",
			error: "#af343a",
			info: "#286477",
		},
	},
	"nocturne": {
		description: "Nocturne · moonlit mint selections and smoky violet (dark)",
		art: ["  ┌──────┐", "  │ NOCTUR│", "  │      │", "  └──────┘"],
		base: {
			neutral: "#111c24",
			text: "#e3f0ee",
			muted: "#a5bab8",
			dim: "#8fa6a6",
			accent: "#8de0cb",
			secondary: "#c5a5ea",
			success: "#97d6a0",
			warning: "#e8c381",
			error: "#ff9b9c",
			info: "#96c8f0",
		},
	},
	"espresso": {
		description: "Espresso · crema selections, rosewood details and coffee ink (dark)",
		art: ["  ┌──────┐", "  │ ESPRES│", "  │      │", "  └──────┘"],
		base: {
			neutral: "#211916",
			text: "#f4e8d7",
			muted: "#c5b2a3",
			dim: "#b09a8a",
			accent: "#edba80",
			secondary: "#d5a0b2",
			success: "#a8cf95",
			warning: "#efd088",
			error: "#f79b8e",
			info: "#94c6d2",
		},
	},
	"velvet": {
		description: "Velvet · jewel pink selections and champagne on aubergine (dark)",
		art: ["  ┌──────┐", "  │ VELVET│", "  │      │", "  └──────┘"],
		base: {
			neutral: "#26182d",
			text: "#f5e7f4",
			muted: "#c6adc8",
			dim: "#b39ab8",
			accent: "#f2a3cb",
			secondary: "#d8c08d",
			success: "#a3d5af",
			warning: "#edca8b",
			error: "#ff9caa",
			info: "#9acddb",
		},
	},
	"deep-sea": {
		description: "Deep sea · bioluminescent cyan and coral on ocean ink (dark)",
		art: ["  ┌──────┐", "  │ DEEP-S│", "  │      │", "  └──────┘"],
		base: {
			neutral: "#0b202b",
			text: "#def1f5",
			muted: "#a1bec7",
			dim: "#8baab7",
			accent: "#76d8e5",
			secondary: "#efa893",
			success: "#9cd7b6",
			warning: "#eaca8a",
			error: "#ff9d9d",
			info: "#9fbfff",
		},
	},
	"copper": {
		description: "Copper · oxidized teal selections and copper highlights (dark)",
		art: ["  ┌──────┐", "  │ COPPER│", "  │      │", "  └──────┘"],
		base: {
			neutral: "#192321",
			text: "#e5eee5",
			muted: "#acbdb2",
			dim: "#96ab9f",
			accent: "#8fd2bd",
			secondary: "#e5af87",
			success: "#a8d39e",
			warning: "#e6c783",
			error: "#f7a09a",
			info: "#a3c7df",
		},
	},
};

export const SKIN_NAMES: string[] = Object.keys(SKINS);

/** Required theme IDs (legacy set + opencode + light). */
export const REQUIRED_THEME_IDS: string[] = [
	"codex",
	"opencode",
	"mono",
	"tokyonight",
	"dracula",
	"gruvbox",
	"nord",
	"catppuccin",
	"solarized",
	"light",
];

/** Every saved/legacy ID plus required IDs must resolve. */
export function isValidTheme(name: string): boolean {
	return SKIN_NAMES.includes(name);
}

/** Raw semantic hex palette for a skin (derived; no Color objects). */
export function semanticPalette(name: string): SemanticPalette | undefined {
	const def = SKINS[name];
	if (!def) return undefined;
	return {
		background: def.base.neutral,
		surface: colorToHex(mixColors(parseColor(def.base.neutral), parseColor(def.base.text), 0.035)),
		raisedSurface: colorToHex(mixColors(parseColor(def.base.neutral), parseColor(def.base.text), 0.065)),
		border: def.base.dim,
		text: def.base.text,
		muted: def.base.muted,
		faint: def.base.dim,
		accent: def.base.accent,
		accentSoft: def.base.accent,
		success: def.base.success,
		warning: def.base.warning,
		danger: def.base.error,
		selection: def.base.accent,
	};
}

const skinColors: Record<string, Record<PaletteName, Color>> = Object.fromEntries(
	Object.entries(SKINS).map(([name, def]) => {
		const neutral = parseColor(def.base.neutral);
		const text = parseColor(def.base.text);
		const full: Record<PaletteName, Color> = {
			text,
			muted: parseColor(def.base.muted),
			dim: parseColor(def.base.dim),
			accent: parseColor(def.base.accent),
			secondary: parseColor(def.base.secondary),
			success: parseColor(def.base.success),
			warning: parseColor(def.base.warning),
			error: parseColor(def.base.error),
			info: parseColor(def.base.info),
			border: mixColors(neutral, text, 0.28),
			userBg: mixColors(neutral, text, 0.1),
			assistantBg: mixColors(neutral, text, 0.06),
			toolBg: mixColors(neutral, text, 0.09),
			toolOkBg: mixColors(neutral, parseColor(def.base.success), 0.13),
			toolErrBg: mixColors(neutral, parseColor(def.base.error), 0.13),
		};
		// Keep readable roles legible on canvas, overlays and tinted cards.
		// Accent hue is retained; adjust lightness only when contrast needs it.
		const surfaces = [neutral, mixColors(neutral, text, 0.035), full.userBg, full.assistantBg, full.toolBg, full.toolOkBg, full.toolErrBg];
		for (const role of ["text", "muted", "dim", "accent", "secondary", "success", "warning", "error", "info"] as const) {
			full[role] = readableInk(full[role], surfaces);
		}
		if (name === "codex") {
			full.border = parseColor(CODEX_SEMANTIC_START.border);
			full.userBg = parseColor(CODEX_SEMANTIC_START.raisedSurface);
			full.toolBg = parseColor(CODEX_SEMANTIC_START.surface);
		}
		return [name, full];
	}),
);

let currentSkin = "codex";

/**
 * Opaque canvas switch. ON (the default) paints every theme surface with its
 * background color exactly as before. OFF emits no background SGR anywhere, so
 * the UI renders on the terminal's default background while every foreground
 * color, bold/dim attribute, and text reset is preserved. Selection and
 * emphasis rows fall back to reverse video + bold (the terminal's own fg/bg
 * pair is a guaranteed-legible swap), so they stay visible with no fill.
 */
let opaqueBackground = true;

/** Central switch for theme background painting. */
export function setOpaqueBackground(enabled: boolean): void {
	opaqueBackground = enabled;
}

/** True when theme surfaces paint an explicit background (the default). */
export function isOpaqueBackground(): boolean {
	return opaqueBackground;
}

/** Background SGR for a color, or "" in transparent mode (single gate). */
export function backgroundColorAnsi(color: Color): string {
	return opaqueBackground ? backgroundAnsi(color, mode) : "";
}

const bgAnsi = backgroundColorAnsi;

function colors(): Record<PaletteName, Color> {
	return skinColors[currentSkin] ?? skinColors["default"]!;
}

/** WCAG relative luminance of a concrete color (0..1). */
function relativeLuminance(color: Color): number {
	const { r, g, b } = colorToRgb(color);
	const channel = (value: number): number => {
		const v = value / 255;
		return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
	};
	return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** WCAG contrast ratio between two colors (1..21). */
function contrastRatio(first: Color, second: Color): number {
	const a = relativeLuminance(first);
	const b = relativeLuminance(second);
	const [high, low] = a >= b ? [a, b] : [b, a];
	return (high + 0.05) / (low + 0.05);
}

/** Preserve the intended hue while reaching AA on every supplied surface. */
function readableInk(ink: Color, surfaces: Color[]): Color {
	if (surfaces.every((surface) => contrastRatio(ink, surface) >= 4.5)) return ink;
	const black = rgbColor(0, 0, 0);
	const white = rgbColor(255, 255, 255);
	const worst = (candidate: Color) => Math.min(...surfaces.map((surface) => contrastRatio(candidate, surface)));
	const target = worst(black) >= worst(white) ? black : white;
	for (let step = 1; step <= 100; step++) {
		const candidate = mixColors(ink, target, step / 100);
		if (worst(candidate) >= 4.5) return candidate;
	}
	return target;
}

/** Selected-row fill: the skin's accent; codex keeps its own selection fill. */
function selectionFillColor(name: string): Color {
	return name === "codex" ? parseColor(CODEX_SEMANTIC_START.selection) : parseColor(SKINS[name]!.base.accent);
}

/**
 * Selected-row ink for a skin: the surface color when it already clears
 * 4.5:1 over the fill (unchanged look for passing skins), otherwise pure
 * black/white — the better of the two is always >= 4.58:1. Cobalt2 (3.62:1)
 * and solarized (4.08:1) previously failed with the surface ink; every skin
 * now meets WCAG AA on selected rows (review round 4, R4-094).
 */
function selectionInkColor(name: string): Color {
	const fill = selectionFillColor(name);
	const def = SKINS[name]!;
	if (name === "codex") return parseColor(def.base.text);
	const surface = parseColor(def.base.neutral);
	if (contrastRatio(surface, fill) >= 4.5) return surface;
	const black = rgbColor(0, 0, 0);
	const white = rgbColor(255, 255, 255);
	return contrastRatio(black, fill) >= contrastRatio(white, fill) ? black : white;
}

const skinSelectionFill: Record<string, Color> = Object.fromEntries(
	SKIN_NAMES.map((name) => [name, selectionFillColor(name)]),
);
const skinSelectionInk: Record<string, Color> = Object.fromEntries(
	SKIN_NAMES.map((name) => [name, selectionInkColor(name)]),
);

function selectionColors(): { ink: Color; fill: Color } {
	return {
		ink: skinSelectionInk[currentSkin] ?? skinSelectionInk["default"]!,
		fill: skinSelectionFill[currentSkin] ?? skinSelectionFill["default"]!,
	};
}

/** Selected-row ink hex for a skin (>= 4.5:1 on the selection fill). */
export function selectionInk(name: string = currentSkin): string {
	return colorToHex(skinSelectionInk[name] ?? skinSelectionInk["default"]!);
}

/** Selected-row fill hex for a skin (accent bg; codex uses its own fill). */
export function selectionFill(name: string = currentSkin): string {
	return colorToHex(skinSelectionFill[name] ?? skinSelectionFill["default"]!);
}

/** Selected-row fill, Codex style: accent bg with a contrast-checked ink. */
function selectedRow(text: string): string {
	if (!opaqueBackground) {
		// No fill: reverse video swaps the terminal's own default fg/bg — a
		// pair the terminal guarantees is legible — and bold marks the row.
		return styleTextWithAnsi(text, undefined, undefined, { bold: true, inverse: true });
	}
	const { ink, fill } = selectionColors();
	return styleTextWithAnsi(text, foregroundAnsi(ink, mode), bgAnsi(fill), {
		bold: true,
	});
}

// ANSI background sequences necessarily contain the ESC control character.
// eslint-disable-next-line no-control-regex
const BG_OPEN = /\x1b\[48;(?:2;\d+;\d+;\d+|5;\d+)m/;
const BG_CLOSE = "\x1b[49m";
const FULL_RESET = "\x1b[0m";

/**
 * Repair a filled line whose content carries nested background resets
 * (e.g. a selection pill inside a filled box): without this, everything
 * after the nested reset falls back to terminal black. Re-asserts the
 * fill background after each reset so padding never leaks.
 */
export function repairFill(bg: PaletteName, filledLine: string): string {
	const open = bgAnsi(colors()[bg]);
	// Transparent canvas: there is no fill to re-assert, and nothing to repair.
	if (!open) return filledLine;
	let body = filledLine;
	if (body.endsWith(BG_CLOSE)) body = body.slice(0, -BG_CLOSE.length);
	const repaired = body
		.split(BG_CLOSE)
		.join(`${BG_CLOSE}${open}`)
		.split(FULL_RESET)
		.join(`${FULL_RESET}${open}`);
	return `${open}${repaired}${BG_CLOSE}`;
}

/** Paint a complete overlay row, including whitespace and nested ANSI resets.
 * Ordinary text always has explicit ink; a terminal's own light/dark defaults
 * must never decide whether a menu label or form value can be read.
 */
export function surfaceLine(line: string, width: number): string {
	const palette = semanticPalette(currentSkin)!;
	const ink = foregroundAnsi(colors().text, mode);
	const fill = bgAnsi(parseColor(palette.surface));
	const padded = `${line}${" ".repeat(Math.max(0, width - visibleWidth(line)))}`;
	const body = padded.split(FULL_RESET).join(`${FULL_RESET}${ink}${fill}`)
		.split("\x1b[39m").join(`\x1b[39m${ink}`)
		.split(BG_CLOSE).join(`${BG_CLOSE}${fill}`);
	return `${ink}${fill}${body}${FULL_RESET}`;
}

/** Keep draft ink explicit across the editor's reverse-video cursor reset. */
export function styleComposerText(line: string): string {
	const ink = foregroundAnsi(colors().text, mode);
	return `${ink}${line.split(FULL_RESET).join(`${FULL_RESET}${ink}`)}\x1b[39m`;
}

/** Remove background color sequences while keeping foreground styling. */
function stripBackground(line: string): string {
	return line.split(BG_OPEN).join("").split(BG_CLOSE).join("");
}

/** Reverse-video opener used by selection rows when no fill is painted. */
const REVERSE_SELECTION = "\x1b[7m";

/** True when a rendered line carries the selection background (or, in
 * transparent mode, the reverse-video selection marker). */
export function isSelectionRow(line: string): boolean {
	if (!opaqueBackground) return line.includes(REVERSE_SELECTION);
	return line.includes(bgAnsi(selectionColors().fill));
}

/**
 * Extend a selected row to a full-width bar: strip the partial background,
 * pad out, and repaint the whole row. Unselected rows stay unfilled. With no
 * opaque canvas the bar becomes a reverse-video + bold row instead of a fill.
 */
export function selectionBar(line: string, width: number): string {
	const stripped = stripBackground(line);
	const padded = `${stripped}${" ".repeat(Math.max(0, width - visibleWidth(line)))}`;
	if (!opaqueBackground) {
		return styleTextWithAnsi(padded, undefined, undefined, { bold: true, inverse: true });
	}
	const { ink, fill } = selectionColors();
	return styleTextWithAnsi(padded, foregroundAnsi(ink, mode), bgAnsi(fill), {
		bold: true,
	});
}

/** Switch the active skin. Throws for unknown names. */
export function setSkin(name: string): void {
	if (!SKINS[name]) throw new Error(`Unknown skin "${name}". Available: ${SKIN_NAMES.join(", ")}`);
	currentSkin = name;
}

/** Currently active skin name. */
export function getSkin(): string {
	return currentSkin;
}

/** One-line description for a skin (for menus). */
export function skinDescription(name: string): string {
	return SKINS[name]?.description ?? "";
}

/** Raw hex palette for a skin (for tests and tooling). */
export function skinBase(name: string): Record<BaseSlot, string> | undefined {
	return SKINS[name]?.base;
}

/** ASCII art lines for a skin (defaults to the active one). */
export function skinArt(name: string = currentSkin): string[] {
	return SKINS[name]?.art ?? SKINS["default"]!.art;
}

const mode = getTerminalColorMode();

function fg(name: PaletteName, attrs: TextAttributes = {}): (text: string) => string {
	return (text) => styleTextWithAnsi(text, foregroundAnsi(colors()[name], mode), undefined, attrs);
}

function bg(name: PaletteName): (text: string) => string {
	return (text) => {
		const fill = bgAnsi(colors()[name]);
		return fill ? styleTextWithAnsi(text, undefined, fill, {}) : text;
	};
}

const plain = (attrs: TextAttributes) => (text: string) => styleTextWithAnsi(text, undefined, undefined, attrs);

export const theme = {
	text: fg("text"),
	bold: plain({ bold: true }),
	muted: fg("muted"),
	dim: fg("dim"),
	accent: fg("accent"),
	accentBold: fg("accent", { bold: true }),
	secondary: fg("secondary"),
	success: fg("success"),
	warning: fg("warning"),
	error: fg("error"),
	info: fg("info"),
	border: fg("border"),
	userBg: bg("userBg"),
	assistantBg: bg("assistantBg"),
	toolBg: bg("toolBg"),
	toolOkBg: bg("toolOkBg"),
	toolErrBg: bg("toolErrBg"),
};

export const markdownTheme: MarkdownTheme = {
	heading: fg("accent", { bold: true }),
	link: fg("accent", { underline: true }),
	linkUrl: fg("dim"),
	code: fg("secondary"),
	codeBlock: fg("text"),
	codeBlockBorder: fg("dim"),
	quote: fg("muted", { italic: true }),
	quoteBorder: fg("dim"),
	hr: fg("dim"),
	listBullet: fg("accent"),
	bold: plain({ bold: true }),
	italic: plain({ italic: true }),
	strikethrough: plain({ strikethrough: true }),
	underline: plain({ underline: true }),
};

export const selectListTheme: SelectListTheme = {
	selectedPrefix: selectedRow,
	selectedText: selectedRow,
	description: fg("muted"),
	scrollInfo: fg("dim"),
	noMatch: fg("dim"),
};

export const editorTheme: EditorTheme = {
	borderColor: fg("border"),
	selectList: selectListTheme,
};
