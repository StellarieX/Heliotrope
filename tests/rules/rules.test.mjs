// Real security-rules tests: they run firestore.rules inside the Firestore
// emulator (unlike the Python suites, which only model the rules).
//
//   npm i --no-save firebase-tools @firebase/rules-unit-testing
//   RULES_PATH=firestore.rules npx firebase emulators:exec --only firestore --project demo-heliotrope "node tests/rules/rules.test.mjs"
//
// Needs Java 21+ for the emulator. Not part of `npm test`/CI (heavy download).
import { initializeTestEnvironment, assertSucceeds, assertFails } from "@firebase/rules-unit-testing";
import { readFileSync } from "node:fs";
import { doc, getDoc, setDoc, deleteDoc, writeBatch, serverTimestamp, collection, addDoc } from "firebase/firestore";

const rules = readFileSync(process.env.RULES_PATH, "utf8");
const env = await initializeTestEnvironment({
  projectId: "demo-heliotrope",
  firestore: { rules, host: "127.0.0.1", port: 8080 },
});

let pass = 0, fail = 0;
async function t(name, fn) {
  try { await fn(); pass++; console.log("  ok   " + name); }
  catch (e) { fail++; console.log("  FAIL " + name + " :: " + (e.message || e).toString().split("\n")[0]); }
}

const alice = env.authenticatedContext("alice").firestore();
const bob = env.authenticatedContext("bob").firestore();
const anon = env.unauthenticatedContext().firestore();

const goodProfile = { username: "alice_1", displayName: "Alice", photoURL: "https://x.test/a.png", occupation: "Student", place: "Home", rooms: 3, onboarded: true, updatedAt: serverTimestamp() };

// --- /users/{uid}
await t("anyone can read a profile (public page)", () => assertSucceeds(getDoc(doc(anon, "users/alice"))));
await t("anon cannot write a profile", () => assertFails(setDoc(doc(anon, "users/alice"), goodProfile)));
await t("other user cannot write my profile", () => assertFails(setDoc(doc(bob, "users/alice"), goodProfile)));
await t("owner can write a valid profile", () => assertSucceeds(setDoc(doc(alice, "users/alice"), goodProfile)));
await t("EMAIL in a profile is rejected", () => assertFails(setDoc(doc(alice, "users/alice"), { ...goodProfile, email: "a@b.c" })));
await t("unknown field is rejected", () => assertFails(setDoc(doc(alice, "users/alice"), { ...goodProfile, isAdmin: true })));
await t("http photoURL is rejected", () => assertFails(setDoc(doc(alice, "users/alice"), { ...goodProfile, photoURL: "http://x.test/a.png" })));
await t("javascript: photoURL is rejected", () => assertFails(setDoc(doc(alice, "users/alice"), { ...goodProfile, photoURL: "javascript:alert(1)" })));
await t("reserved username in profile is rejected", () => assertFails(setDoc(doc(alice, "users/alice"), { ...goodProfile, username: "admin" })));
await t("uppercase username in profile is rejected", () => assertFails(setDoc(doc(alice, "users/alice"), { ...goodProfile, username: "Alice" })));
await t("oversized displayName is rejected", () => assertFails(setDoc(doc(alice, "users/alice"), { ...goodProfile, displayName: "x".repeat(81) })));
await t("non-number rooms is rejected", () => assertFails(setDoc(doc(alice, "users/alice"), { ...goodProfile, rooms: "lots" })));
await t("merge-update keeps validity (onboarding step)", () => assertSucceeds(setDoc(doc(alice, "users/alice"), { occupation: "Homeowner", place: "Campus", rooms: null, onboarded: true }, { merge: true })));
await t("owner can delete own profile", () => assertSucceeds(deleteDoc(doc(alice, "users/alice"))));
await t("other user cannot delete my profile", async () => {
  await env.withSecurityRulesDisabled(async (c) => setDoc(doc(c.firestore(), "users/alice"), { username: "alice_1" }));
  await assertFails(deleteDoc(doc(bob, "users/alice")));
});

// legacy doc that still has an email: owner may strip it, may not keep writing it
await t("legacy email doc: removing the email succeeds", async () => {
  await env.withSecurityRulesDisabled(async (c) => setDoc(doc(c.firestore(), "users/alice"), { username: "alice_1", email: "old@x.test" }));
  const { updateDoc, deleteField } = await import("firebase/firestore");
  await assertSucceeds(updateDoc(doc(alice, "users/alice"), { email: deleteField() }));
});
await t("legacy email doc: any write that keeps the email fails", async () => {
  await env.withSecurityRulesDisabled(async (c) => setDoc(doc(c.firestore(), "users/alice"), { username: "alice_1", email: "old@x.test" }));
  await assertFails(setDoc(doc(alice, "users/alice"), { displayName: "New" }, { merge: true }));
});

// --- jobs stay private
await t("owner reads/writes own jobs", async () => {
  await assertSucceeds(addDoc(collection(alice, "users/alice/jobs"), { name: "EV", powerKw: 7 }));
});
await t("other user cannot read my jobs", () => assertFails(getDoc(doc(bob, "users/alice/jobs/x"))));
await t("anon cannot read jobs", () => assertFails(getDoc(doc(anon, "users/alice/jobs/x"))));

// --- /usernames/{name}
await t("anyone can read the registry", () => assertSucceeds(getDoc(doc(anon, "usernames/whoever"))));
await t("claim a valid name", () => assertSucceeds(setDoc(doc(alice, "usernames/alice_1"), { uid: "alice", updatedAt: serverTimestamp() })));
await t("claim for someone else's uid fails", () => assertFails(setDoc(doc(bob, "usernames/bobs_name"), { uid: "alice" })));
await t("RESERVED name 'dashboard' is rejected", () => assertFails(setDoc(doc(alice, "usernames/dashboard"), { uid: "alice" })));
await t("RESERVED name 'api' is rejected", () => assertFails(setDoc(doc(alice, "usernames/api"), { uid: "alice" })));
await t("too-short name is rejected", () => assertFails(setDoc(doc(alice, "usernames/ab"), { uid: "alice" })));
await t("uppercase name is rejected", () => assertFails(setDoc(doc(alice, "usernames/Alice"), { uid: "alice" })));
await t("claim with extra fields is rejected", () => assertFails(setDoc(doc(alice, "usernames/alice_2"), { uid: "alice", note: "x" })));
await t("cannot overwrite another user's claim", () => assertFails(setDoc(doc(bob, "usernames/alice_1"), { uid: "bob" })));
await t("cannot delete another user's claim", () => assertFails(deleteDoc(doc(bob, "usernames/alice_1"))));
await t("owner can delete own claim", () => assertSucceeds(deleteDoc(doc(alice, "usernames/alice_1"))));

// --- the app's real onboarding write shapes
await env.clearFirestore();
await t("onboarding: claim + profile in one batch", async () => {
  const b = writeBatch(alice);
  b.set(doc(alice, "usernames/alice_9"), { uid: "alice", updatedAt: serverTimestamp() });
  b.set(doc(alice, "users/alice"), { displayName: "Alice", photoURL: null, username: "alice_9", updatedAt: serverTimestamp() }, { merge: true });
  await assertSucceeds(b.commit());
});
await t("onboarding: jobs + onboarded flag in one batch", async () => {
  const b = writeBatch(alice);
  b.set(doc(collection(alice, "users/alice/jobs")), { name: "Geyser", powerKw: 2, readyBy: "06:00", flexHours: 2 });
  b.set(doc(alice, "users/alice"), { occupation: "Student", place: "Home", rooms: null, onboarded: true }, { merge: true });
  await assertSucceeds(b.commit());
});

// --- load shape and private settings
await t("full app load shape is accepted", () =>
  assertSucceeds(addDoc(collection(alice, "users/alice/jobs"), {
    name: "Geyser bank", kind: "Water heating", shiftable: true, powerKw: 6, readyBy: "06:00",
    flexHours: 1, jobType: "THERMAL", tempMinC: 40, tempMaxC: 65, confidence: 0.92,
    createdAt: new Date().toISOString(),
  })));
await t("load with an unknown field is rejected", () =>
  assertFails(addDoc(collection(alice, "users/alice/jobs"), { name: "EV", powerKw: 7, payload: "x".repeat(10) })));
await t("load with an invalid job type is rejected", () =>
  assertFails(addDoc(collection(alice, "users/alice/jobs"), { name: "EV", jobType: "ROCKET" })));
await t("load with an absurd power is rejected", () =>
  assertFails(addDoc(collection(alice, "users/alice/jobs"), { name: "EV", powerKw: 1e9 })));
await t("load without a name is rejected", () =>
  assertFails(addDoc(collection(alice, "users/alice/jobs"), { powerKw: 2 })));
await t("owner saves max power in private settings", () =>
  assertSucceeds(setDoc(doc(alice, "users/alice/settings/prefs"), { maxPowerKw: 25, updatedAt: serverTimestamp() })));
await t("other user cannot read my settings", () => assertFails(getDoc(doc(bob, "users/alice/settings/prefs"))));
await t("settings reject other documents", () =>
  assertFails(setDoc(doc(alice, "users/alice/settings/other"), { maxPowerKw: 25 })));
await t("settings reject unknown fields", () =>
  assertFails(setDoc(doc(alice, "users/alice/settings/prefs"), { maxPowerKw: 25, email: "a@b.c" })));

await env.cleanup();
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
