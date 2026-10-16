// Inline images are represented by small markers in the editor and transcript.
// Only explicitly dropped files or requested clipboard contents are read.
import { spawn } from "node:child_process";
import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";

export interface ImageAttachment { name: string; mediaType: string; data: string }
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const MAX_TOTAL_BYTES = 20 * 1024 * 1024;
const MAX_IMAGES = 5;

function mediaType(data: Buffer): string | undefined {
    if (data.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) return "image/png";
    if (data[0] === 255 && data[1] === 216 && data[2] === 255) return "image/jpeg";
    if (/^GIF8[79]a$/.test(data.subarray(0, 6).toString("ascii"))) return "image/gif";
    if (data.subarray(0, 4).toString("ascii") === "RIFF" && data.subarray(8, 12).toString("ascii") === "WEBP") return "image/webp";
    return undefined;
}

function fromBytes(data: Buffer, name: string): ImageAttachment {
    if (data.length > MAX_IMAGE_BYTES) throw new Error("Image exceeds 5 MiB. Resize it and try again.");
    const type = mediaType(data);
    if (!type) throw new Error("Unsupported image format. Use PNG, JPEG, GIF or WebP.");
    return { name: Array.from(name, (char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127 ? "_" : char).join("").slice(0, 255), mediaType: type, data: data.toString("base64") };
}

/** Parse a terminal file drop without executing the shell-escaped text. */
export function imagePaths(text: string): string[] | null {
    if (text.length > 20_000 || !text.trim()) return null;
    const tokens: string[] = [];
    let token = "", quote = "", escaped = false;
    for (const char of text.trim()) {
        if (escaped) { token += char; escaped = false; }
        else if (char === "\\" && quote !== "'") escaped = true;
        else if (quote) { if (char === quote) quote = ""; else token += char; }
        else if (char === "'" || char === '"') quote = char;
        else if (/\s/.test(char)) { if (token) { tokens.push(token); token = ""; } }
        else token += char;
    }
    if (quote || escaped) return null;
    if (token) tokens.push(token);
    const paths = tokens.map((value) => {
        if (value.startsWith("file://")) {
            try { return fileURLToPath(value); } catch { return ""; }
        }
        return value.startsWith("~/") ? homedir() + value.slice(1) : value;
    });
    return paths.length && paths.every((path) => isAbsolute(path) && /\.(png|jpe?g|gif|webp)$/i.test(path)) ? paths : null;
}

export async function readImageFile(path: string): Promise<ImageAttachment> {
    const file = await open(path, constants.O_RDONLY | (constants.O_NONBLOCK ?? 0));
    try {
        const stat = await file.stat();
        if (!stat.isFile()) throw new Error("Drop a regular image file.");
        if (stat.size > MAX_IMAGE_BYTES) throw new Error("Image exceeds 5 MiB. Resize it and try again.");
        // Bound the read even if a file grows after stat.
        const buffer = Buffer.alloc(Math.min(stat.size + 1, MAX_IMAGE_BYTES + 1));
        let used = 0;
        while (used < buffer.length) {
            const read = await file.read(buffer, used, buffer.length - used, null);
            if (!read.bytesRead) break;
            used += read.bytesRead;
        }
        return fromBytes(buffer.subarray(0, used), basename(path));
    } finally { await file.close(); }
}

export type ClipboardCommand = (command: string, args: string[]) => Promise<Buffer>;
/**
 * Clipboard read (pbpaste/xclip/…) kill bound. Matches the copy-path bound in
 * clipboard.ts: the old 3000 ms cap was breached under full-suite load, and a
 * slow-but-working paste would be reported to the user as an empty clipboard
 * (the 3.0769 s flake in the copy path is the same failure class). 8s keeps a
 * genuinely stuck backend fail-fast while clearing the observed worst case.
 */
export const CLIPBOARD_READ_TIMEOUT_MS = 8000;
export const runClipboardCommand: ClipboardCommand = (command, args) => new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
    const chunks: Buffer[] = [];
    let size = 0, settled = false;
    const timer = setTimeout(() => finish(new Error("Clipboard read timed out.")), CLIPBOARD_READ_TIMEOUT_MS);
    const finish = (error?: Error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (error) { child.kill(); reject(error); }
        else resolve(Buffer.concat(chunks));
    };
    child.on("error", (error) => finish(error));
    child.stdout.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size > Math.ceil(MAX_IMAGE_BYTES * 4 / 3) + 1024) finish(new Error("Clipboard image exceeds 5 MiB."));
        else chunks.push(chunk);
    });
    child.on("close", (code) => finish(code === 0 ? undefined : new Error("Clipboard has no image.")));
});

// AppKit also handles copied TIFF screenshots, converting them to PNG.
const MAC_IMAGE_SCRIPT = `ObjC.import('AppKit');
const pasteboard = $.NSPasteboard.generalPasteboard;
let data = pasteboard.dataForType('public.png');
if (!data || data.isNil()) {
    const tiff = pasteboard.dataForType('public.tiff');
    if (tiff && !tiff.isNil()) {
        const bitmap = $.NSBitmapImageRep.imageRepWithData(tiff);
        if (bitmap && !bitmap.isNil()) data = bitmap.representationUsingTypeProperties(4, $.NSDictionary.dictionary);
    }
}
if (data && !data.isNil()) {
    if (Number(data.length) > ${MAX_IMAGE_BYTES}) throw new Error('Image exceeds 5 MiB');
    ObjC.unwrap(data.base64EncodedStringWithOptions(0));
} else { ''; }`;

export async function clipboardImage(platform = process.platform, run: ClipboardCommand = runClipboardCommand): Promise<ImageAttachment | null> {
    const commands: Array<[string, string[], boolean]> = platform === "darwin" ? [
        ["osascript", ["-l", "JavaScript", "-e", MAC_IMAGE_SCRIPT], true],
    ] : platform === "win32" ? [
        ["powershell.exe", ["-NoProfile", "-NonInteractive", "-STA", "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; $i=[Windows.Forms.Clipboard]::GetImage(); if($i){$s=New-Object IO.MemoryStream; $i.Save($s,[Drawing.Imaging.ImageFormat]::Png); [Convert]::ToBase64String($s.ToArray()); $s.Dispose(); $i.Dispose()}"], true],
    ] : [
        ["wl-paste", ["--no-newline", "--type", "image/png"], false],
        ["xclip", ["-selection", "clipboard", "-t", "image/png", "-o"], false],
    ];
    for (const [command, args, encoded] of commands) {
        try {
            const result = await run(command, args);
            if (!result.length) continue;
            const bytes = encoded ? Buffer.from(result.toString().trim(), "base64") : result;
            return fromBytes(bytes, "clipboard.png");
        } catch (error) {
            if (error instanceof Error && /exceeds|timed out/i.test(error.message)) throw error;
        }
    }
    return null;
}

export async function clipboardText(platform = process.platform): Promise<string> {
    const candidates: Array<[string, string[]]> = platform === "darwin" ? [["pbpaste", []]] : platform === "win32" ? [
        ["powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"]],
    ] : [["wl-paste", ["--no-newline", "--type", "text"]], ["xclip", ["-selection", "clipboard", "-o"]]];
    for (const [command, args] of candidates) {
        try { return (await runClipboardCommand(command, args)).toString("utf8").slice(0, 20_000); } catch { /* Try the next available clipboard backend. */ }
    }
    return "";
}

export class ImageDraft {
    private readonly images = new Map<number, ImageAttachment>();
    private nextId = 1;

    add(image: ImageAttachment): string {
        if (this.images.size >= MAX_IMAGES) throw new Error("Attach at most 5 images per message.");
        const total = [...this.images.values(), image].reduce((sum, item) => sum + Buffer.byteLength(item.data, "base64"), 0);
        if (total > MAX_TOTAL_BYTES) throw new Error("Images exceed the 20 MiB total attachment limit.");
        const id = this.nextId++;
        this.images.set(id, image);
        return `[image ${id}]`;
    }

    addMany(images: ImageAttachment[]): string[] {
        const ids = [...this.images.keys()];
        const next = this.nextId;
        try { return images.map((image) => this.add(image)); }
        catch (error) {
            for (const id of this.images.keys()) if (!ids.includes(id)) this.images.delete(id);
            this.nextId = next;
            throw error;
        }
    }

    prune(text: string): void {
        const retained = new Set([...text.matchAll(/\[image (\d+)\]/g)].map((match) => Number(match[1])));
        for (const id of this.images.keys()) if (!retained.has(id)) this.images.delete(id);
    }

    prepare(text: string): { text: string; images: ImageAttachment[] } {
        // A text-only draft can refer to images already stored in the session.
        // Current attachments still require every marker to resolve locally.
        if (!this.images.size) return { text, images: [] };
        const ids = new Map<number, number>();
        const images: ImageAttachment[] = [];
        const rendered = text.replace(/\[image (\d+)\]/g, (_marker, raw: string) => {
            const id = Number(raw);
            const image = this.images.get(id);
            if (!image) throw new Error("This image is no longer available. Drop or paste it again.");
            if (!ids.has(id)) { images.push(image); ids.set(id, images.length); }
            return `[image ${ids.get(id)}]`;
        });
        return { text: rendered, images };
    }

    clear(): void { this.images.clear(); this.nextId = 1; }
}
