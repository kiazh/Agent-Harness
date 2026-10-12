import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { ImageDraft, imagePaths, readImageFile, clipboardImage, runClipboardCommand } from "../src/images.ts";

const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64");

test("Finder drops support quoted paths, escaped spaces and file URLs", () => {
    assert.deepEqual(imagePaths("'/tmp/a image.png' /tmp/b\\ image.jpg "), ["/tmp/a image.png", "/tmp/b image.jpg"]);
    assert.deepEqual(imagePaths("file:///tmp/my%20image.png"), ["/tmp/my image.png"]);
    assert.equal(imagePaths("explain /tmp/a.png"), null);
    assert.equal(imagePaths("/mcp"), null);
    assert.equal(imagePaths("https://example.com/a.png"), null);
});

test("dropped image content is detected from bytes and only live markers are submitted", async () => {
    const root = await mkdtemp(join(tmpdir(), "machina-image-test-"));
    try {
        const path = join(root, "a picture.jpeg");
        await writeFile(path, PNG);
        const image = await readImageFile(path);
        assert.equal(image.mediaType, "image/png");
        assert.equal(image.data, PNG.toString("base64"));
        const draft = new ImageDraft();
        const first = draft.add(image);
        const second = draft.add(image);
        assert.equal(first, "[image 1]");
        assert.equal(second, "[image 2]");
        const submitted = draft.prepare(`describe ${second}`);
        assert.equal(submitted.text, "describe [image 1]");
        assert.deepEqual(submitted.images, [image]);
        assert.deepEqual(draft.prepare("no images").images, []);
        draft.clear();
        // Markers in a text-only follow-up refer to persisted session images.
        assert.deepEqual(draft.prepare(`what colour is ${first}?`), { text: "what colour is [image 1]?", images: [] });
        draft.add(image);
        assert.throws(() => draft.prepare("[image 2]"), /no longer available/i);
    } finally { await rm(root, { recursive: true, force: true }); }
});

test("arbitrary data, directories and oversize files are refused", async () => {
    const root = await mkdtemp(join(tmpdir(), "machina-image-test-"));
    try {
        const path = join(root, "bad.png");
        await writeFile(path, "not an image");
        await assert.rejects(readImageFile(path), /format/i);
        await assert.rejects(readImageFile(root), /file/i);
        await writeFile(path, Buffer.alloc(5 * 1024 * 1024 + 1));
        await assert.rejects(readImageFile(path), /5 MiB/i);
    } finally { await rm(root, { recursive: true, force: true }); }
});

test("native clipboard bytes become an inline image; text clipboard returns no image", async () => {
    const calls: string[] = [];
    const image = await clipboardImage("linux", async (command) => { calls.push(command); return PNG; });
    assert.equal(image?.mediaType, "image/png");
    assert.ok(calls.includes("wl-paste"));
    assert.equal(await clipboardImage("linux", async () => { throw new Error("no image"); }), null);
});

test("image attachment count and total byte limits are applied before dispatch", () => {
    const draft = new ImageDraft();
    const image = { name: "a.png", mediaType: "image/png", data: PNG.toString("base64") };
    const markers = Array.from({ length: 5 }, () => draft.add(image));
    assert.throws(() => draft.add(image), /5 images/i);
    assert.equal(draft.prepare(markers.join(" ")).images.length, 5);
});

test("macOS AppKit reads PNG from an isolated pasteboard without touching the user's clipboard", { skip: process.platform !== "darwin" }, async () => {
    const image = await clipboardImage("darwin", async (command, args) => {
        const source = args.at(-1)!;
        const setup = `ObjC.import('AppKit'); const board=$.NSPasteboard.pasteboardWithUniqueName;
board.setDataForType($.NSData.alloc.initWithBase64EncodedStringOptions('${PNG.toString("base64")}',0),'public.png');\n`;
        const script = setup + source.replace("$.NSPasteboard.generalPasteboard", "board").replace("ObjC.unwrap(data.base64EncodedStringWithOptions(0));", "board.releaseGlobally; ObjC.unwrap(data.base64EncodedStringWithOptions(0));");
        return runClipboardCommand(command, ["-l", "JavaScript", "-e", script]);
    });
    assert.equal(image?.data, PNG.toString("base64"));
});
