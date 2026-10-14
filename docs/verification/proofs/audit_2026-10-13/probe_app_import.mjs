try {
  const mod = await import("/Users/shadow/machina/ui/src/app.ts");
  console.log("app import ok; App exported:", typeof mod.App);
  console.log("prototype writeFile:", typeof mod.App?.prototype?.writeFile);
} catch (err) {
  console.log("app import failed:", err.message.split("\n")[0]);
}
