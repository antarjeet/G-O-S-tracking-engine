const { MongoClient } = require('mongodb');
const crypto = require('crypto');

const MONGO_URL = process.env.MONGO_URL || 'mongodb://localhost:27017/';
const DB_NAME = process.env.MONGO_DB_NAME || 'ai_gos_hud';
const SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000; // 30 days
const CI_COLLATION = { locale: 'en', strength: 2 };

let client = null;
let usersCol = null;
let sessionsCol = null;
let connecting = null;

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function connectWithRetry(attempts = 5) {
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const c = new MongoClient(MONGO_URL);
      await c.connect();
      return c;
    } catch (err) {
      if (attempt === attempts) throw err;
      const backoffMs = 500 * 2 ** (attempt - 1);
      console.warn(`[DB] Connection attempt ${attempt}/${attempts} failed (${err.message}); retrying in ${backoffMs}ms...`);
      await delay(backoffMs);
    }
  }
}

function connect() {
  if (!connecting) {
    connecting = (async () => {
      try {
        client = await connectWithRetry();
      } catch (err) {
        // Let a later call retry from scratch instead of staying stuck on
        // this permanently-rejected promise for the rest of the process's life.
        connecting = null;
        throw err;
      }
      client.on('error', (err) => console.error('[DB] MongoClient error:', err));
      client.on('close', () => console.warn('[DB] MongoDB connection closed.'));
      const db = client.db(DB_NAME);
      usersCol = db.collection('users');
      sessionsCol = db.collection('sessions');
      await usersCol.createIndex({ username: 1 }, { unique: true, collation: CI_COLLATION });
      await usersCol.createIndex({ email: 1 }, { unique: true, collation: CI_COLLATION });
      await usersCol.createIndex({ id: 1 }, { unique: true });
      await sessionsCol.createIndex({ token: 1 }, { unique: true });
      // TTL index: MongoDB's background task deletes session documents once
      // expiresAt has passed, so expired sessions clean themselves up. The
      // findSession() expiry check below is a synchronous backstop for the
      // window before that background sweep runs (it's not instant).
      await sessionsCol.createIndex({ expiresAt: 1 }, { expireAfterSeconds: 0 });
      console.log(`[DB] Connected to MongoDB (${MONGO_URL}${DB_NAME})`);
    })();
  }
  return connecting;
}

async function close() {
  if (client) {
    await client.close();
    client = null;
  }
  connecting = null;
  usersCol = null;
  sessionsCol = null;
}

async function findUserByUsername(username) {
  await connect();
  return usersCol.findOne({ username }, { collation: CI_COLLATION });
}

async function findUserByEmail(email) {
  await connect();
  return usersCol.findOne({ email }, { collation: CI_COLLATION });
}

async function findUserById(id) {
  await connect();
  return usersCol.findOne({ id });
}

async function createUser({ username, email, passwordHash, isAdmin = false }) {
  await connect();
  const user = {
    id: crypto.randomUUID(),
    username,
    email,
    passwordHash,
    isAdmin,
    createdAt: new Date().toISOString()
  };
  await usersCol.insertOne(user);
  return user;
}

async function getAllUsers() {
  await connect();
  return usersCol.find({}).toArray();
}

async function setUserAdmin(id, isAdmin) {
  await connect();
  return usersCol.findOneAndUpdate({ id }, { $set: { isAdmin } }, { returnDocument: 'after' });
}

async function deleteUser(id) {
  await connect();
  const result = await usersCol.deleteOne({ id });
  if (result.deletedCount === 0) return false;
  await sessionsCol.deleteMany({ userId: id });
  return true;
}

async function createSession(userId) {
  await connect();
  const token = crypto.randomBytes(32).toString('hex');
  const session = { token, userId, createdAt: Date.now(), expiresAt: new Date(Date.now() + SESSION_TTL_MS) };
  await sessionsCol.insertOne(session);
  return session;
}

async function findSession(token) {
  await connect();
  const session = await sessionsCol.findOne({ token });
  if (!session) return null;
  if (session.expiresAt < new Date()) {
    await deleteSession(token);
    return null;
  }
  return session;
}

async function deleteSession(token) {
  await connect();
  await sessionsCol.deleteOne({ token });
}

module.exports = {
  findUserByUsername,
  findUserByEmail,
  findUserById,
  createUser,
  getAllUsers,
  setUserAdmin,
  deleteUser,
  createSession,
  findSession,
  deleteSession,
  close
};
