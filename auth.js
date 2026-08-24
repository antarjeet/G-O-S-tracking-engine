const express = require('express');
const bcrypt = require('bcryptjs');
const rateLimit = require('express-rate-limit');
const db = require('./db');

// Brute-force mitigation on the two credential-checking routes only — not
// /me, /logout, or the admin routes, which don't take a guessable secret.
const credentialLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 30,
  standardHeaders: true,
  legacyHeaders: false,
  message: { success: false, message: 'Too many attempts. Please try again later.' }
});

const COOKIE_NAME = 'ai_gos_session';
const COOKIE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000; // 30 days, matches db.js session TTL

// Cross-origin cookie (frontend on Vite's port, backend on 5000) requires
// SameSite=None, which in turn requires Secure — fine here since the
// backend is HTTPS-only anyway.
const COOKIE_OPTS = { httpOnly: true, secure: true, sameSite: 'none', maxAge: COOKIE_MAX_AGE_MS };

const USERNAME_RE = /^[a-zA-Z0-9_]{3,24}$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function publicUser(user) {
  return { id: user.id, username: user.username, email: user.email, isAdmin: !!user.isAdmin };
}

async function ensureAdminFromEnv() {
  const { ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD } = process.env;
  if (!ADMIN_USERNAME && !ADMIN_EMAIL && !ADMIN_PASSWORD) {
    return;
  }
  if (!ADMIN_USERNAME || !ADMIN_EMAIL || !ADMIN_PASSWORD) {
    console.warn('[Auth] ADMIN_USERNAME, ADMIN_EMAIL and ADMIN_PASSWORD must all be set in backend/.env to seed an admin account — skipping.');
    return;
  }
  const existing = await db.findUserByUsername(ADMIN_USERNAME);
  if (existing) {
    if (!existing.isAdmin) {
      await db.setUserAdmin(existing.id, true);
      console.log(`[Auth] Promoted existing account "${ADMIN_USERNAME}" to admin.`);
    }
    return;
  }
  const passwordHash = await bcrypt.hash(ADMIN_PASSWORD, 10);
  await db.createUser({ username: ADMIN_USERNAME, email: ADMIN_EMAIL, passwordHash, isAdmin: true });
  console.log(`[Auth] Created admin account "${ADMIN_USERNAME}" from backend/.env.`);
}

async function requireAuth(req, res, next) {
  try {
    const token = req.cookies && req.cookies[COOKIE_NAME];
    const session = token && (await db.findSession(token));
    if (!session) {
      return res.status(401).json({ success: false, message: 'Not authenticated' });
    }
    const user = await db.findUserById(session.userId);
    if (!user) {
      return res.status(401).json({ success: false, message: 'Not authenticated' });
    }
    req.user = user;
    next();
  } catch (err) {
    next(err);
  }
}

function requireAdmin(req, res, next) {
  if (!req.user || !req.user.isAdmin) {
    return res.status(403).json({ success: false, message: 'Admin access required' });
  }
  next();
}

// Same check, usable for Socket.io's handshake (no Express req/res there).
async function getUserFromToken(token) {
  const session = token && (await db.findSession(token));
  if (!session) return null;
  return (await db.findUserById(session.userId)) || null;
}

function parseCookieHeader(header) {
  const out = {};
  if (!header) return out;
  header.split(';').forEach((part) => {
    const idx = part.indexOf('=');
    if (idx === -1) return;
    const key = part.slice(0, idx).trim();
    const value = part.slice(idx + 1).trim();
    if (key) out[key] = decodeURIComponent(value);
  });
  return out;
}

const router = express.Router();

router.post('/signup', credentialLimiter, async (req, res, next) => {
  try {
    const { username, email, password } = req.body || {};
    if (!username || !email || !password) {
      return res.status(400).json({ success: false, message: 'Username, email and password are required' });
    }
    if (!USERNAME_RE.test(username)) {
      return res.status(400).json({ success: false, message: 'Username must be 3-24 characters: letters, numbers, underscore' });
    }
    if (!EMAIL_RE.test(email)) {
      return res.status(400).json({ success: false, message: 'Enter a valid email address' });
    }
    if (password.length < 8) {
      return res.status(400).json({ success: false, message: 'Password must be at least 8 characters' });
    }
    if (await db.findUserByUsername(username)) {
      return res.status(409).json({ success: false, message: 'That username is already taken' });
    }
    if (await db.findUserByEmail(email)) {
      return res.status(409).json({ success: false, message: 'That email is already registered' });
    }

    const passwordHash = await bcrypt.hash(password, 10);
    const user = await db.createUser({ username, email, passwordHash });
    const session = await db.createSession(user.id);
    res.cookie(COOKIE_NAME, session.token, COOKIE_OPTS);
    res.json({ success: true, user: publicUser(user) });
  } catch (err) {
    next(err);
  }
});

router.post('/login', credentialLimiter, async (req, res, next) => {
  try {
    const { username, password } = req.body || {};
    if (!username || !password) {
      return res.status(400).json({ success: false, message: 'Username and password are required' });
    }
    const user = (await db.findUserByUsername(username)) || (await db.findUserByEmail(username));
    if (!user) {
      return res.status(401).json({ success: false, message: 'Incorrect username or password' });
    }
    const match = await bcrypt.compare(password, user.passwordHash);
    if (!match) {
      return res.status(401).json({ success: false, message: 'Incorrect username or password' });
    }
    const session = await db.createSession(user.id);
    res.cookie(COOKIE_NAME, session.token, COOKIE_OPTS);
    res.json({ success: true, user: publicUser(user) });
  } catch (err) {
    next(err);
  }
});

router.post('/logout', async (req, res, next) => {
  try {
    const token = req.cookies && req.cookies[COOKIE_NAME];
    if (token) await db.deleteSession(token);
    res.clearCookie(COOKIE_NAME, { httpOnly: true, secure: true, sameSite: 'none' });
    res.json({ success: true });
  } catch (err) {
    next(err);
  }
});

router.get('/me', requireAuth, (req, res) => {
  res.json({ success: true, user: publicUser(req.user) });
});

// Nothing else in this app has differentiated permissions yet, so this is
// the one thing "admin" currently means: seeing and managing the account
// list. Every route here is chained requireAuth + requireAdmin.

async function adminCount() {
  const users = await db.getAllUsers();
  return users.filter((u) => u.isAdmin).length;
}

router.get('/admin/users', requireAuth, requireAdmin, async (req, res, next) => {
  try {
    const users = await db.getAllUsers();
    res.json({ success: true, users: users.map(publicUser) });
  } catch (err) {
    next(err);
  }
});

router.post('/admin/users/:id/promote', requireAuth, requireAdmin, async (req, res, next) => {
  try {
    const user = await db.setUserAdmin(req.params.id, true);
    if (!user) return res.status(404).json({ success: false, message: 'User not found' });
    res.json({ success: true, user: publicUser(user) });
  } catch (err) {
    next(err);
  }
});

router.post('/admin/users/:id/demote', requireAuth, requireAdmin, async (req, res, next) => {
  try {
    const target = await db.findUserById(req.params.id);
    if (!target) return res.status(404).json({ success: false, message: 'User not found' });
    if (target.isAdmin && (await adminCount()) <= 1) {
      return res.status(400).json({ success: false, message: 'Cannot demote the last remaining admin' });
    }
    const user = await db.setUserAdmin(req.params.id, false);
    res.json({ success: true, user: publicUser(user) });
  } catch (err) {
    next(err);
  }
});

router.delete('/admin/users/:id', requireAuth, requireAdmin, async (req, res, next) => {
  try {
    const target = await db.findUserById(req.params.id);
    if (!target) return res.status(404).json({ success: false, message: 'User not found' });
    if (target.isAdmin && (await adminCount()) <= 1) {
      return res.status(400).json({ success: false, message: 'Cannot delete the last remaining admin' });
    }
    await db.deleteUser(req.params.id);
    res.json({ success: true });
  } catch (err) {
    next(err);
  }
});

module.exports = { router, requireAuth, requireAdmin, getUserFromToken, parseCookieHeader, COOKIE_NAME, ensureAdminFromEnv };
