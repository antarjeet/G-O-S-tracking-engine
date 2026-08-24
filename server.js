require('dotenv').config();

const express = require('express');
const https = require('https');
const os = require('os');
const crypto = require('crypto');
const { Server } = require('socket.io');
const cors = require('cors');
const helmet = require('helmet');
const compression = require('compression');
const morgan = require('morgan');
const cookieParser = require('cookie-parser');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const QRCode = require('qrcode');
const { router: authRouter, requireAuth, getUserFromToken, parseCookieHeader, COOKIE_NAME, ensureAdminFromEnv } = require('./auth');
const db = require('./db');

// After this point the process's state is undefined (Node's own guidance) —
// log with a stack trace and exit rather than keep serving requests on a
// process that might be half-broken. Unhandled rejections are logged only,
// matching Node 20's default (they don't crash the process on their own).
process.on('uncaughtException', (err) => {
  console.error('[FATAL] Uncaught exception:', err);
  process.exit(1);
});
process.on('unhandledRejection', (reason) => {
  console.error('[FATAL] Unhandled promise rejection:', reason);
});
// Phone camera capture (getUserMedia) requires a "secure context", and the
// phone reaches this server over the LAN by IP (not localhost), so this
// server must speak HTTPS. getOrCreateCertificate() auto-provisions a
// trusted cert via mkcert when available (see certSetup.js).
const { getOrCreateCertificate, getRootCaPath } = require('./certSetup');

function getLanIp() {
  const interfaces = os.networkInterfaces();
  for (const name of Object.keys(interfaces)) {
    for (const iface of interfaces[name] || []) {
      if (iface.family === 'IPv4' && !iface.internal) {
        return iface.address;
      }
    }
  }
  return 'localhost';
}

async function main() {
  await ensureAdminFromEnv();

  const app = express();
  const { cert, key, trusted: certTrusted } = await getOrCreateCertificate();
  const server = https.createServer({ cert, key });
  const rootCaPath = certTrusted ? getRootCaPath() : null;
  // Session cookies need credentials on cross-origin requests (frontend on
  // Vite's port, backend on 5000), which browsers refuse to send unless the
  // server echoes back the exact request origin (not '*') with
  // Access-Control-Allow-Credentials: true. Reflecting whatever origin asks
  // is appropriate here — this is a personal local-network tool, not a
  // public API with untrusted origins to defend against.
  const corsOptions = { origin: (origin, cb) => cb(null, origin || true), credentials: true };

  // Registered BEFORE `new Server(server, ...)` below, on purpose: Socket.io's
  // attach() step captures whatever 'request' listeners already exist on
  // `server`, removes them, and installs a single listener that does a
  // synchronous req.url path-prefix check (no I/O — immune to proxy/tunnel
  // latency) to decide, per request, whether to handle it itself or call this
  // one. That guarantees exactly one handler ever touches a given request.
  // Registering `app` as a *second*, independent 'request' listener instead
  // (the previous approach here) doesn't get that guarantee: Node fires every
  // 'request' listener unconditionally, so for a /socket.io/* request that
  // Socket.io already answered and ended, Express's separate listener still
  // ran afterward and crashed trying to touch the finished response
  // (ERR_HTTP_HEADERS_SENT) — reliably for synchronous responses, and even
  // for async ones once Express's own deferred 404 fallback (finalhandler)
  // ended up writing after Socket.io had since sent its headers.
  server.on('request', app);

  const io = new Server(server, {
    cors: corsOptions,
    // The telemetry payload is a mostly-incompressible base64 JPEG; spending
    // CPU trying to deflate it every frame only adds latency for no benefit.
    perMessageDeflate: false
  });

  // CSP/HSTS/CORP are disabled explicitly: this app deliberately reflects
  // any origin (see corsOptions above) and serves a plain inline-script page
  // (phone-cam.html) with no nonce/hash setup, which a default CSP would
  // break.
  app.use(helmet({ contentSecurityPolicy: false, crossOriginResourcePolicy: false, hsts: false }));
  app.use(compression());
  app.use(cors(corsOptions));
  app.use(express.json());
  app.use(cookieParser());
  app.use(morgan(process.env.NODE_ENV === 'production' ? 'combined' : 'dev', {
    skip: (req) => req.path === '/api/phone-frame/latest'
  }));
  app.use(express.static(path.join(__dirname, 'public')));
  app.use('/api/auth', authRouter);

  const PORT = process.env.PORT || 5000;

  // Resolved once here (rather than inside startPythonEngine, which runs on
  // every /api/engine/toggle call) since they never change at runtime.
  const venvPythonPath = path.join(__dirname, 'engine', '.venv', 'Scripts', 'python.exe');
  const ultimateScriptPath = path.join(__dirname, 'engine', 'ultimate_gesture_control.py');
  const fallbackBridgePath = path.join(__dirname, 'engine', 'ai_engine_bridge.py');
  const resolvedPythonCmd = fs.existsSync(venvPythonPath) ? venvPythonPath : 'python';
  const resolvedScriptPath = fs.existsSync(ultimateScriptPath) ? ultimateScriptPath : fallbackBridgePath;

  let pythonProcess = null;
  let engineRunning = false;
  let currentCameraSource = '';
  let latestTelemetry = {
    status: 'Idle',
    source: 'Pending',
    fps: 0,
    latency: 0,
    confidence: 0,
    gesture: 'IDLE',
    landmarks: [],
    pointer: { x: 1039, y: 291 },
    frame: null,
    engineActive: false
  };

  let activePhoneSessionId = null;
  let latestPhoneFrame = null;
  let latestPhoneFrameTime = 0;
  let phoneConnected = false;
  const PHONE_FRAME_STALE_MS = 3000;

  setInterval(() => {
    const stale = !latestPhoneFrameTime || (Date.now() - latestPhoneFrameTime > PHONE_FRAME_STALE_MS);
    if (phoneConnected && stale) {
      phoneConnected = false;
      io.emit('phone-status', { connected: false });
    }
  }, 1000);

  function startPythonEngine(cameraSource, mouseSpeed) {
    if (pythonProcess) return;

    const pythonCmd = resolvedPythonCmd;
    const scriptPath = resolvedScriptPath;

    currentCameraSource = (cameraSource || '').trim();
    // 0.5x-3x, matching MOUSE_SPEED_MIN/MAX in ultimate_gesture_control.py
    // (which re-clamps this independently — the source of truth for the
    // valid range lives there, not here).
    const parsedSpeed = Number(mouseSpeed);
    const startupMouseSpeed = Number.isFinite(parsedSpeed) ? parsedSpeed : 1.0;
    console.log(`[Express Backend] Spawning Python Engine Command: ${pythonCmd} ${scriptPath}` +
      (currentCameraSource ? ` (camera: ${currentCameraSource})` : ' (camera: local webcam)') +
      ` (mouse speed: ${startupMouseSpeed}x)`);
    engineRunning = true;

    try {
      pythonProcess = spawn(pythonCmd, ['-u', scriptPath], {
        cwd: path.join(__dirname, 'engine'),
        env: {
          ...process.env,
          // No OS window: stream the processed camera frame to the web HUD
          // instead, and accept K/H/G/etc. shortcuts as stdin commands.
          AI_GOS_HEADLESS: '1',
          // Optional phone camera ('phone' = poll this backend's latest phone
          // frame) or IP-camera URL / device index, instead of the PC webcam.
          AI_GOS_CAMERA_SOURCE: currentCameraSource,
          AI_GOS_BACKEND_PORT: String(PORT),
          // Initial cursor-speed multiplier — the "Mouse Speed" slider in
          // the web HUD's Phone & Settings tab. Adjustable live afterward
          // via the SET_MOUSE_SPEED: engine command (see /api/engine/command).
          AI_GOS_MOUSE_SPEED: String(startupMouseSpeed)
        }
      });

      // JSON telemetry (now including a base64 JPEG frame) arrives as one
      // line per frame, but a single stdout 'data' chunk can split a line
      // mid-message. Buffer partial lines across chunks instead of dropping them.
      let stdoutBuffer = '';
      pythonProcess.stdout.on('data', (data) => {
        stdoutBuffer += data.toString();
        const lines = stdoutBuffer.split('\n');
        stdoutBuffer = lines.pop();
        lines.forEach((line) => {
          if (line.trim()) {
            try {
              const parsed = JSON.parse(line.trim());
              latestTelemetry = { ...parsed, engineActive: true };
              io.emit('ai-frame', latestTelemetry);
            } catch (err) {
            }
          }
        });
      });

      pythonProcess.stderr.on('data', (data) => {
        console.error(`[Python stderr]: ${data.toString()}`);
      });

      pythonProcess.on('close', (code) => {
        console.log(`[Python Engine Process] Exited with code ${code}.`);
        pythonProcess = null;
        engineRunning = false;
        latestTelemetry.engineActive = false;
        io.emit('ai-frame', { ...latestTelemetry, engineActive: false });
        io.emit('engine-status', { active: false });
      });

      // Without this handler, a spawn failure (e.g. bad path, missing
      // interpreter) emits an unhandled 'error' event that crashes the
      // entire Express process instead of just failing to start the engine.
      pythonProcess.on('error', (err) => {
        console.error('Failed to spawn Python process:', err);
        pythonProcess = null;
        engineRunning = false;
        latestTelemetry.engineActive = false;
        io.emit('ai-frame', { ...latestTelemetry, engineActive: false });
        io.emit('engine-status', { active: false });
      });
    } catch (err) {
      console.error('Failed to spawn Python process:', err);
    }
  }

  function stopPythonEngine() {
    if (pythonProcess) {
      console.log(`[Express Backend] Terminating Python AI Engine process...`);
      pythonProcess.kill('SIGTERM');
      pythonProcess = null;
      engineRunning = false;
      latestTelemetry.engineActive = false;
      io.emit('ai-frame', { ...latestTelemetry, engineActive: false });
      io.emit('engine-status', { active: false });
    }
  }

  function sendEngineCommand(command) {
    if (pythonProcess && pythonProcess.stdin.writable) {
      pythonProcess.stdin.write(`${command}\n`);
      return true;
    }
    return false;
  }

  app.get('/api/status', requireAuth, (req, res) => {
    res.json({
      backend: 'Node.js Express + Socket.io Active',
      engineActive: engineRunning,
      telemetry: latestTelemetry,
      phoneConnected
    });
  });

  app.post('/api/engine/toggle', requireAuth, (req, res) => {
    if (engineRunning) {
      stopPythonEngine();
      res.json({ success: true, engineActive: false, message: 'Python AI Engine HALTED' });
    } else {
      startPythonEngine(req.body && req.body.cameraSource, req.body && req.body.mouseSpeed);
      res.json({ success: true, engineActive: true, message: 'Python AI Engine STARTED' });
    }
  });

  app.post('/api/engine/command', requireAuth, (req, res) => {
    const { command } = req.body || {};
    if (!command) {
      return res.status(400).json({ success: false, message: 'Missing command' });
    }
    const sent = sendEngineCommand(command);
    if (sent) {
      res.json({ success: true });
    } else {
      res.status(409).json({ success: false, message: 'Engine is not running' });
    }
  });

  app.post('/api/mode', requireAuth, (req, res) => {
    const { mode } = req.body;
    io.emit('mode-change', { mode });
    res.json({ success: true, mode });
  });

  // Whether this server's HTTPS cert is a trusted mkcert-issued one (in
  // which case installing the CA below removes the phone's warning
  // entirely) or an untrusted selfsigned fallback (in which case there's no
  // CA worth installing — "Advanced -> Proceed" on the leaf cert is the
  // only option). Unauthenticated like /phone-cam/:sessionId itself: the
  // phone reaches this before it has any session of its own, and none of
  // this is sensitive — it's metadata about a public certificate.
  app.get('/api/cert-info', (req, res) => {
    res.json({ trusted: !!certTrusted, caDownloadAvailable: !!rootCaPath });
  });

  // Serves mkcert's public root CA certificate so a phone can install it as
  // a trusted authority — the private key backing it never leaves this
  // machine's mkcert install, so handing out this file grants no more than
  // "trust HTTPS certs this dev machine issues for itself", the same trust
  // this PC's own browser already extends via `mkcert -install`.
  app.get('/root-ca.pem', (req, res) => {
    if (!rootCaPath) {
      return res.status(404).send('No trusted CA available on this server (it is using a self-signed certificate).');
    }
    res.setHeader('Content-Type', 'application/x-x509-ca-cert');
    res.setHeader('Content-Disposition', 'attachment; filename="ai-gos-ca.pem"');
    res.sendFile(rootCaPath);
  });

  app.get('/api/phone-session/new', requireAuth, async (req, res) => {
    activePhoneSessionId = crypto.randomUUID();
    latestPhoneFrame = null;
    latestPhoneFrameTime = 0;
    phoneConnected = false;
    const url = `https://${getLanIp()}:${PORT}/phone-cam/${activePhoneSessionId}`;
    try {
      const qrDataUrl = await QRCode.toDataURL(url, { margin: 1, width: 320 });
      res.json({ sessionId: activePhoneSessionId, url, qr: qrDataUrl, certTrusted: !!certTrusted, caDownloadAvailable: !!rootCaPath });
    } catch (err) {
      res.status(500).json({ success: false, message: 'Failed to generate QR code' });
    }
  });

  app.get('/api/phone-session/status', requireAuth, (req, res) => {
    res.json({ connected: phoneConnected, sessionId: activePhoneSessionId });
  });

  // Explicit user-initiated disconnect (as opposed to the staleness timeout
  // above, which only notices a phone that went quiet on its own). Ends the
  // session immediately: the phone's next frame POST gets a 410 and its
  // capture page stops trying, and every connected browser is told right
  // away instead of waiting up to PHONE_FRAME_STALE_MS.
  app.post('/api/phone-session/disconnect', requireAuth, (req, res) => {
    activePhoneSessionId = null;
    latestPhoneFrame = null;
    latestPhoneFrameTime = 0;
    if (phoneConnected) {
      phoneConnected = false;
      io.emit('phone-status', { connected: false });
    }
    res.json({ success: true });
  });

  app.get('/phone-cam/:sessionId', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'phone-cam.html'));
  });

  app.post('/api/phone-frame/:sessionId', express.raw({ type: '*/*', limit: '3mb' }), (req, res) => {
    if (req.params.sessionId !== activePhoneSessionId) {
      return res.status(410).json({ success: false, message: 'Session ended' });
    }
    latestPhoneFrame = req.body;
    latestPhoneFrameTime = Date.now();
    if (!phoneConnected) {
      phoneConnected = true;
      io.emit('phone-status', { connected: true });
    }
    res.json({ success: true });
  });

  // The phone only pushes a new frame a few times a second, but Python can
  // poll far faster than that. Without the ?since= check below, every poll
  // between real phone frames would still return the same bytes, making the
  // pipeline re-process (and count as "new") an unchanged image — inflating
  // the reported FPS with meaningless repeats and making MediaPipe redo work
  // on a frame it already saw.
  //
  // This is an internal endpoint (only the locally-spawned Python process
  // calls it) with no user in the loop to authenticate, so it's restricted
  // to loopback instead of requireAuth — otherwise anyone on the LAN could
  // fetch it directly and see the last frame the phone uploaded.
  app.get('/api/phone-frame/latest', (req, res) => {
    const remote = req.socket.remoteAddress || '';
    const isLoopback = remote === '127.0.0.1' || remote === '::1' || remote === '::ffff:127.0.0.1';
    if (!isLoopback) {
      return res.status(403).end();
    }
    const stale = !latestPhoneFrameTime || (Date.now() - latestPhoneFrameTime > PHONE_FRAME_STALE_MS);
    if (!latestPhoneFrame || stale) {
      return res.status(204).end();
    }
    const since = Number(req.query.since) || 0;
    if (latestPhoneFrameTime <= since) {
      return res.status(204).end();
    }
    res.set('Content-Type', 'image/jpeg');
    res.set('X-Frame-Time', String(latestPhoneFrameTime));
    res.send(latestPhoneFrame);
  });

  // Anything passed to next(err) by a route above (or thrown synchronously
  // inside one) lands here instead of Express's own default handler, which
  // would otherwise leak stack traces.
  app.use((err, req, res, next) => {
    console.error('[Express error]', err);
    if (res.headersSent) return next(err);
    res.status(500).json({
      success: false,
      message: process.env.NODE_ENV === 'production' ? 'Internal server error' : (err && err.message) || 'Internal server error'
    });
  });

  // The socket handshake carries the browser's cookies just like a normal
  // HTTP request, but Socket.io doesn't parse them — do that manually and
  // reject the connection outright if there's no valid session, so the
  // realtime channel is gated exactly like the REST API.
  io.use(async (socket, next) => {
    try {
      const cookies = parseCookieHeader(socket.handshake.headers.cookie);
      const user = await getUserFromToken(cookies[COOKIE_NAME]);
      if (!user) {
        return next(new Error('Not authenticated'));
      }
      socket.user = user;
      next();
    } catch (err) {
      next(err);
    }
  });

  io.on('connection', (socket) => {
    console.log(`[Socket.io] React Frontend Connected: ${socket.id} (user: ${socket.user.username})`);
    socket.emit('ai-frame', { ...latestTelemetry, engineActive: engineRunning });
    socket.emit('phone-status', { connected: phoneConnected });

    socket.on('toggle-engine', (payload) => {
      if (engineRunning) stopPythonEngine();
      else startPythonEngine(payload && payload.cameraSource, payload && payload.mouseSpeed);
    });

    socket.on('engine-command', (command) => {
      if (typeof command === 'string') sendEngineCommand(command);
    });

    socket.on('disconnect', () => {
      console.log(`[Socket.io] React Frontend Disconnected: ${socket.id}`);
    });
  });

  server.listen(PORT, () => {
    console.log(`=======================================================`);
    console.log(`🚀 Express AI-GOS Server active on https://localhost:${PORT}`);
    console.log(`   LAN address for phone pairing: https://${getLanIp()}:${PORT}`);
    if (certTrusted) {
      console.log(`   ✅ Trusted local certificate (mkcert) — no warning on this PC.`);
      console.log(`      Phones can install the same CA from the "Secure connection" panel on the pairing page.`);
    } else {
      console.log(`   ⚠️  Using a self-signed certificate — run "npm run setup:https" for a trusted one (needs mkcert).`);
    }
    console.log(`=======================================================`);
  });

  let shuttingDown = false;
  async function shutdown(signal) {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log(`\n[Server] ${signal} received — shutting down...`);
    stopPythonEngine();
    io.close();
    server.close(() => console.log('[Server] HTTPS server closed.'));
    try {
      await db.close();
    } catch (err) {
      console.error('[Server] Error closing DB connection:', err);
    }
    process.exit(0);
  }
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
}

main().catch((err) => {
  console.error('Failed to start AI-GOS backend:', err);
  process.exit(1);
});
