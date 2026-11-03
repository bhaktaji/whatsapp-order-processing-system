import { spawn, type ChildProcess } from "child_process";
import express, { type Request, type Response } from "express";
import path from "path";
import { fileURLToPath } from "url";
import { createServer as createViteServer } from "vite";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = 3000;
const FASTAPI_PORT = 8000;
const FASTAPI_BASE = `http://127.0.0.1:${FASTAPI_PORT}`;

let fastapiProc: ChildProcess | null = null;

function startFastApiBackend(): void {
  const env = {
    ...process.env,
    PYTHONPATH: path.join(__dirname, "backend"),
    APP_ENV: process.env.APP_ENV || "development",
    DATABASE_URL: process.env.DATABASE_URL || `sqlite:///${path.join(__dirname, "orderbridge_dev.db")}`,
    JWT_SECRET: process.env.JWT_SECRET || "orderbridge_local_preview_jwt_secret_2026_32b",
    META_VERIFY_TOKEN: process.env.META_VERIFY_TOKEN || "orderbridge_local_verify_token_2026",
    META_APP_SECRET: process.env.META_APP_SECRET || "orderbridge_local_hmac_secret_2026",
    PHONE_ENCRYPTION_KEY: process.env.PHONE_ENCRYPTION_KEY || "orderbridge_local_phone_key_32b!",
  };

  fastapiProc = spawn(
    "python3",
    [
      "-m",
      "uvicorn",
      "app.main:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(FASTAPI_PORT),
    ],
    {
      cwd: __dirname,
      env: {
        ...env,
        PATH: `/tmp/ob_venv/bin:/root/.local/bin:${process.env.PATH || "/usr/local/bin:/usr/bin:/bin"}`,
      },
      stdio: ["ignore", "pipe", "pipe"],
    }
  );

  fastapiProc.stdout?.on("data", (chunk: Buffer) => {
    process.stdout.write(`[fastapi] ${chunk.toString()}`);
  });
  fastapiProc.stderr?.on("data", (chunk: Buffer) => {
    process.stderr.write(`[fastapi] ${chunk.toString()}`);
  });
  fastapiProc.on("exit", (code) => {
    console.warn(`[fastapi] exited with code ${code}; restarting in 2s...`);
    setTimeout(startFastApiBackend, 2000);
  });
}

async function proxyToFastApi(req: Request, res: Response): Promise<void> {
  const targetUrl = `${FASTAPI_BASE}${req.originalUrl}`;
  const headers: Record<string, string> = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (typeof v === "string" && k.toLowerCase() !== "host" && k.toLowerCase() !== "content-length") {
      headers[k] = v;
    }
  }

  const hasBody = !["GET", "HEAD"].includes(req.method.toUpperCase());
  const rawBody = hasBody && (req as Request & { rawBody?: Buffer }).rawBody
    ? (req as Request & { rawBody?: Buffer }).rawBody
    : undefined;

  for (let attempt = 0; attempt < 12; attempt++) {
    try {
      const upstream = await fetch(targetUrl, {
        method: req.method,
        headers,
        body: rawBody ? new Uint8Array(rawBody) : undefined,
      });

      res.status(upstream.status);
      const ct = upstream.headers.get("content-type");
      if (ct) {
        res.setHeader("Content-Type", ct);
      }
      const buf = Buffer.from(await upstream.arrayBuffer());
      res.send(buf);
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 350));
    }
  }

  res.status(502).json({
    error: "FastAPI backend is warming up",
    target: targetUrl,
  });
}

async function startServer(): Promise<void> {
  startFastApiBackend();

  const app = express();

  app.use(
    express.raw({
      type: "*/*",
      limit: "5mb",
    }),
    (req: Request, _res: Response, next) => {
      if (Buffer.isBuffer(req.body)) {
        (req as Request & { rawBody?: Buffer }).rawBody = req.body;
      }
      next();
    }
  );

  app.all("/api/*splat", proxyToFastApi);
  app.all("/webhooks/*splat", proxyToFastApi);
  app.get("/health", proxyToFastApi);
  app.get("/ready", proxyToFastApi);

  if (process.env.NODE_ENV !== "production") {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: "spa",
    });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(__dirname, "dist");
    app.use(express.static(distPath));
    app.get("*splat", (_req, res) => {
      res.sendFile(path.join(distPath, "index.html"));
    });
  }

  app.listen(PORT, "0.0.0.0", () => {
    console.log(`OrderBridge Full-Stack Server listening on http://0.0.0.0:${PORT} (proxying API to FastAPI :${FASTAPI_PORT})`);
  });
}

startServer();
