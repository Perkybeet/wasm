// Fixture server for the WASM real-machine integration harness.
//
// Deliberately dependency-free (node:http only) so `npm install` never has to
// fetch anything, since some of the scenarios that exercise this app run
// with no working internet inside the container.
//
// GET  /        -> "ok <version>", where <version> is read from ./VERSION.
// POST /upload   -> writes the request body to ./uploads/<name> and returns
//                    the file name. Used to prove that `wasm update` does not
//                    delete files an application wrote into its own tree.

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const PORT = process.env.PORT || 3000;
const ROOT = __dirname;
const UPLOADS_DIR = path.join(ROOT, "uploads");

function readVersion() {
  try {
    return fs.readFileSync(path.join(ROOT, "VERSION"), "utf8").trim();
  } catch (err) {
    return "unknown";
  }
}

function handleUpload(req, res) {
  const chunks = [];
  req.on("data", (chunk) => chunks.push(chunk));
  req.on("end", () => {
    try {
      fs.mkdirSync(UPLOADS_DIR, { recursive: true });
      const body = Buffer.concat(chunks);
      const name = "upload.txt";
      fs.writeFileSync(path.join(UPLOADS_DIR, name), body);
      res.writeHead(200, { "Content-Type": "text/plain" });
      res.end(`uploaded ${name} (${body.length} bytes)\n`);
    } catch (err) {
      res.writeHead(500, { "Content-Type": "text/plain" });
      res.end(`upload failed: ${err.message}\n`);
    }
  });
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);

  if (req.method === "GET" && url.pathname === "/") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end(`ok ${readVersion()}\n`);
    return;
  }

  if (req.method === "POST" && url.pathname === "/upload") {
    handleUpload(req, res);
    return;
  }

  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("not found\n");
});

server.listen(PORT, () => {
  console.log(`wasm-it-node-app listening on ${PORT}, version ${readVersion()}`);
});
