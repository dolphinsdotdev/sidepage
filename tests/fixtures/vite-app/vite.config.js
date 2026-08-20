// Fixture for tests/test_proxy_frameworks.py — deliberately the bare
// default config (no `server.allowedHosts`), matching "a Vite dev server
// the user already has running, unmodified" — the actual scenario
// `sidepage proxy` wraps. The test itself writes a second, explicitly
// wildcard-allowlisted copy to demonstrate the documented fix, rather
// than this fixture pre-solving its own caveat.
import { defineConfig } from "vite";

export default defineConfig({});
