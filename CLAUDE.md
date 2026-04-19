# Cardinal Nest Monitor — Project Memory for Claude

> Read this file FIRST when starting work on this project. It captures everything you need to be useful immediately.

---

## What this project is

Real-time AI threat-detection for a backyard Northern Cardinal nest in Marietta, Georgia. The user has a Blink Outdoor camera (`Hummer_CAGE_CAM`) pointed at a cardinal nest in a rose bush near the back door. A **Brown Thrasher** has been observed tampering with the eggs at least once. The user asked us to build a system that pings Discord **only when something actionable is happening at the nest** — predator at nest, direct nest interaction, or egg disappearance. False positives (yard motion, mockingbirds, brief mother absences) must NOT generate alerts.

This is **emotionally important to the user.** It is not a fun project. The cardinal eggs are at real risk; the user is a protective parent of the nest and has consented to operational complexity (battery swaps every ~10–14 days, ~$90/month Anthropic spend) in exchange for reliable monitoring.

**As of 2026-04-15 the system is tuned for single-tier Sonnet + dynamic absence-aware cadence:**
- Blink motion detection is OFF in the Blink app (mom's movements on the nest were triggering constant false-positive motion events). `motion_loop` still runs but finds nothing.
- Every snap → `claude-sonnet-4-6` directly (no Haiku prefilter). Single model call per snap.
- Cadence is **dynamic**: 5 min default, **1 min when `state.in_absence=True`** (peak predation risk window), 30 min during quiet hours (23:00–05:00 EDT).
- See "Cadence configuration" below for current values.

The full design rationale is in `~/.claude/plans/reactive-tickling-rose.md`.

---

## Architecture in 30 seconds

**Two independent launchd services** (decoupled 2026-04-15 — see §20):

```
DOWNLOADER SERVICE (com.cardinalnest.downloader):
  cam.snap_picture() → fresh JPEG (~6s) → data/spool/pending/ (atomic rename)
  Cadence is dynamic (reads state.sqlite read-only via WAL mode):
    if in quiet hours (23:00–05:00 local):    1800s = 30 min
    elif state.in_absence:                     60s = 1 min  (Pattern A)
    else:                                      300s = 5 min
  If analyzer is down >10 min: falls back to safe 60s constant.

ANALYZER SERVICE (com.cardinalnest.analyzer):
  Polls data/spool/pending/ every 1s → claims newest snap first
    → analyzer.analyze() via claude-sonnet-4-6 (~$0.01)
    → if CRITICAL/HIGH: blind Opus 4.6 verification (~$0.05)
    → NestObservation → state.py → events.py → notifier.py → Discord
    → evidence/YYYY-MM-DD/HH-MM-SS_<sev>_<species>/
    → feed_worker posts to feed channel (non-blocking, bounded queue)
  Stale snaps (>30s old) route to #nest-backfill Discord channel.
  Snaps >30 min old are dropped (Anthropic cost cap).

MOTION POLL LOOP (every 15s, currently no-op — runs inside downloader):
  blink.refresh() → looks for new clips. With Blink motion OFF, finds nothing.
```

**Reaction time** (scheduled-snap only, since motion is off):
- Mom on nest (default 5-min cadence): up to 5 min
- Mom absent (1-min cadence — Pattern A): up to 1 min ← peak risk window
- Quiet hours: up to 30 min

**Zero-gap redeploys:** restarting the analyzer (`launchctl kickstart -k com.cardinalnest.analyzer`) never interrupts snap capture. Snaps queue in the spool and drain when the analyzer comes back. See §20 for the full playbook.

**If motion detection is re-enabled**, motion events trigger out-of-cycle snaps with ~60–120s floor (clip-upload bottleneck, see Hard-won knowledge §11).

## Cadence configuration (current production values, 2026-04-16)

| Setting | Value | Why |
|---|---|---|
| `ANALYZER_MODEL` | `claude-sonnet-4-6` | Single-tier. Was Opus 4.6. Sonnet is ~5x cheaper with no meaningful accuracy regression for threat detection. |
| `VERIFICATION_MODEL` | `claude-opus-4-7` | Upgraded from 4.6 on 2026-04-16 (4.7 released same week). Better vision, new high-res image support. Used for blind second-opinion on CRITICAL/HIGH. |
| `MULTI_IMAGE_ANALYSIS` | `true` | Analyzer receives 3 crops per snap (full + center-zoom + overview) for better recall on subtle thrasher features. Roughly 2-3x input tokens. |
| `PREFILTER_MODEL` | `claude-haiku-4-5-20251001` | **Unused in single-tier mode.** Kept in config for easy re-enable. |
| `SNAP_INTERVAL_SECONDS` | 300 (5 min) | Default day cadence when mom is on nest. |
| `ABSENCE_SNAP_INTERVAL_SECONDS` | 60 (1 min) | **Pattern A.** Fallback cadence when `state.in_absence=True` and burst window expired. |
| `BURST_SNAP_INTERVAL_SECONDS` | 30 | **Burst.** First N seconds after mom leaves. Peak predation risk. Thrasher attacks are 4s. |
| `BURST_DURATION_SECONDS` | 180 (3 min) | How long burst cadence applies after absence starts. After this, relaxes to ABSENCE interval. |
| `QUIET_HOURS` | 23:00-05:00 | 6h overnight quiet window. Includes raccoon/opossum peak hours. |
| `QUIET_SNAP_INTERVAL_SECONDS` | 1800 (30 min) | Sparse overnight baseline. Overrides absence interval during quiet hours. |
| `FORCED_OPUS_INTERVAL_SECONDS` | 900 (15 min) | **Unused in single-tier mode** (no tier escalation to force). Kept for re-enable. |
| `MOTION_POLL_SECONDS` | 15 | Blink API rate-limit floor. Don't go lower. |
| `ACTIVE_HOURS` | 00:00-23:59 | 24/7 (quiet hours handle overnight). |
| Blink motion detection (in Blink app) | **OFF** | User disabled 2026-04-15; mom's nest movements were firing constant false triggers. `motion_loop` is a no-op until re-enabled. |

Daily volume at these settings (assuming ~95% on-nest / ~5% absent during day):
- Day on-nest (18h × 12 snaps/h) ≈ **205 scheduled snaps/day**
- Day absent (~55 snaps during 1.2h of foraging) ≈ **55 snaps/day**
- Quiet (6h × 2/h) ≈ **12 scheduled snaps/day**
- Motion-triggered (Blink motion off) ≈ **0 snaps/day**
- **Total: ~285 snaps/day**, ~$180-270/mo Anthropic spend (with multi-image analysis on; ~$90/mo if disabled).

---

## File map (where to look for what)

```
src/cardinal_nest_monitor/
  schema.py           Pydantic models + Anthropic tool_use schemas (NestObservation, PrefilterResult, AlertDecision, Severity, NestState). Single source of truth.
  config.py           pydantic-settings. Loads .env. get_settings() is cached.
  blink_client.py     connect() + motion_loop() + snap_loop() + download_clip(). The Blink/blinkpy stuff lives here.
  prefilter.py        Tier-1 Haiku 4.5 wrapper. Single image in → PrefilterResult out.
  analyzer.py         Tier-2 Opus 4.6 wrapper. Single image in → NestObservation out.
  state.py            SQLite-backed StateStore. Tracks observations, derived state, alerts table for cooldowns.
  events.py           Pure rules engine. evaluate(obs, state, store, ts) → AlertDecision | None.
  notifier.py         Discord webhook (multipart attachments, severity-colored embeds).
  evidence.py         Per-event directory writer.
  main.py             Wires everything. Pipeline class + run_combined/run_downloader/run_analyzer + heartbeat scheduler + battery scheduler + signal handlers.
  __main__.py         python -m entrypoint. --role={combined,downloader,analyzer} flag.
  _image.py           cv2 downscale + base64 encode helper (used by prefilter + analyzer).
  spool.py            Atomic-rename file queue. write_snap/claim_next/mark_complete/recover_stranded/drop_stale.
  downloader_loop.py  Blink→spool producer. Own watchdog + lifecycle embeds. See §20.
  analyzer_loop.py    Spool→Pipeline consumer. Live/backfill routing. Runs analyzer-side schedulers. See §20.
  tools/
    test_discord.py   `python -m cardinal_nest_monitor.tools.test_discord` — sends 🧪 embed.
    dryrun.py         `python -m ...tools.dryrun --image PATH [--escalate]` — pipeline test on local JPEG.
    pause.py          `python -m ...tools.pause [minutes] | --clear` — write/clear pause.lock.

launchd/com.cardinalnest.monitor.plist       Legacy combined LaunchAgent (rollback). Installed at ~/Library/LaunchAgents/ (edit paths in the plist to match your system).
launchd/com.cardinalnest.downloader.plist    Downloader-only LaunchAgent. See §20.
launchd/com.cardinalnest.analyzer.plist      Analyzer-only LaunchAgent. See §20.
.env                                          gitignored. Real secrets here.
.env.example                                  Committed template (no secrets). Documents all env vars.
blink_credentials.json                        gitignored. Persisted Blink auth token (saved by --auth-only).
data/state.sqlite                             gitignored. Runtime state (observations, alerts, derived).
data/spool/                                   gitignored. Atomic-rename spool for downloader→analyzer handoff. See §20.
evidence/YYYY-MM-DD/                          gitignored. One directory per snap.
evidence/reference/                           NOT gitignored (intentionally). Hand-curated regression test images for prompt changes — see Hard-won knowledge §15.
tests/                                        pytest. Run with `python -m pytest tests/`. 91 unit + 31 integration = 122 tests (including spool + backfill guards), all should pass.
```

---

## Hard-won knowledge: things that took us hours to figure out

### 1. blinkpy 0.25.5 API gotchas

- **`BlinkSetupError` lives in `blinkpy.blinkpy`, NOT `blinkpy.auth`.** Earlier docs say auth — they're wrong for 0.25.5. We import: `from blinkpy.blinkpy import Blink, BlinkSetupError`.
- **2FA completion uses `auth.complete_2fa_login(pin)`, NOT `auth.send_auth_key()`.** The `send_auth_key` method existed in blinkpy 0.22 and was removed/renamed for the OAuth-v2 flow in 0.25.x.
- **After `complete_2fa_login`, you must manually call:** `blink.setup_urls()` (sync), `await blink.get_homescreen()`, `await blink.setup_post_verify()`. Without these, `blink.cameras` stays empty.
- **Motion events are polling-only.** No webhooks exist. `blink.refresh()` minimum cadence is ~15s — anything faster risks 429s and account lockouts.

### 2. The Blink 2FA file-polling pattern (CRITICAL — this is non-obvious)

Blink emails a 6-digit PIN that must be entered to complete OAuth. The naïve `input()` approach fails in three ways:

- **`!` prefix in Claude Code** runs commands without a TTY → `input()` raises `EOFError` immediately.
- **Echo-piping the PIN** (`echo 123456 | python -m ...`) only works if you already know the PIN BEFORE running. But each run generates new OAuth CSRF state, so the PIN from one invocation can't validate against a different invocation's session.
- **Each failed attempt burns a `client_id` slot** on the Blink account (cap is ~10 concurrent clients). Don't waste these.

**Solution implemented in `blink_client.py`: `_read_2fa_pin()` falls through three sources:**
1. `BLINK_PIN` env var (instant)
2. Real interactive stdin if attached to a TTY
3. **Polling `/tmp/cardinal_nest_blink_pin` for up to 5 minutes** ← this is the path that works in non-interactive contexts

Full first-time auth workflow:

```bash
# Run in background — script triggers PIN email, then waits
rm -f /tmp/cardinal_nest_blink_pin
source venv/bin/activate
python -m cardinal_nest_monitor --auth-only &

# Check email at the BLINK_USERNAME inbox for the latest PIN
# Drop it into the file:
echo "123456" > /tmp/cardinal_nest_blink_pin

# Background script picks it up within 2s, completes auth, exits.
# blink_credentials.json is now written and reusable indefinitely.
```

After first auth: subsequent runs reuse `blink_credentials.json` and don't need 2FA. Token can silently expire (~once a year per blinkpy issue history) — the script catches `"unexpected mimetype"` errors and tries `auth.login()` + `setup_post_verify()` to refresh. If that fails, you'll need to re-run the 2FA flow above.

### 3. Anthropic workspace billing trap

The first API key lived in a workspace with $0 credits even though the billing page showed $20. Anthropic accounts can have multiple workspaces; **each workspace has its own credit pool** and API keys are workspace-scoped. The fix was creating a fresh key in the funded workspace.

If you see `BadRequestError: Your credit balance is too low` despite credits visibly existing, check:
1. https://console.anthropic.com/settings/keys — note which Workspace the failing key belongs to
2. https://console.anthropic.com/settings/workspaces — check that workspace's billing
3. Either move credits OR create a new key in the funded workspace

### 4. Egg-loss rule needs PRE-record state

`StateStore.record()` updates `last_known_egg_count` BEFORE returning. If `events.evaluate()` is called with the post-record state, the egg-loss comparison `current < state.last_known_egg_count` becomes `current < current` (always false). **Always call `store.get_state()` BEFORE `store.record()` and pass that pre-state to `evaluate()`.** This is what `Pipeline.on_image()` does in main.py.

The same applies to the `mother_returned` rule (rule 5 in events.py) — it needs to see `in_absence=True` before record() flips it back to False.

### 5. Active hours gate the snap loop, not the entire system

When outside `ACTIVE_HOURS`, the SCHEDULED snap loop sleeps 60s and rechecks. But:
- **Motion events still poll every 15s** and trigger snaps even outside active hours
- **Heartbeat and battery schedulers still tick** as scheduled
- **Discord notifier still works** (so manual sends, alerts on motion-triggered snaps, etc. still go through)

**Current setting: `ACTIVE_HOURS=00:00-23:59` (24/7).** The user explicitly chose 24/7 because mammalian predators (raccoons, opossums, snakes) are nocturnal — cutting overnight monitoring would be exactly the wrong tradeoff for nest protection. The original plan default of `06:00-21:00` was wrong-headed and got overridden in production. **Don't suggest narrowing this without an explicit cost-driven reason** — protecting eggs is the whole point.

### 6. SIGKILL vs SIGTERM shutdown

The `notifier.send_system_message(title="🔴 Cardinal Nest Monitor offline", ...)` call in `main.py`'s finally block only runs on SIGTERM (catchable). If a process dies via SIGKILL or `kill -9`, no offline embed fires. **Bash's `TaskStop` may use SIGKILL.** `launchctl bootout` sends SIGTERM and should trigger a clean shutdown sequence. If you're stopping the system to test, use `launchctl bootout gui/$(id -u)/com.cardinalnest.monitor` rather than `kill -9`.

### 7. The plan-mode trap

Several times during development, plan mode got reactivated mid-task (Shift+Tab toggle), preventing edits. The Agent tool also inherits plan-mode permissions when spawned, so parallel implementation agents got stuck writing sub-plans they couldn't execute. Workaround: read their generated plan files (in `~/.claude/plans/`) and apply the writes from the main session.

### 8. Haiku hallucinates cardinals on IR night images (historical — Haiku was dropped 2026-04-15)

**Status as of 2026-04-15:** we switched to single-tier Sonnet 4.6 and dropped Haiku entirely. This section is kept as historical context in case anyone re-introduces Haiku as a prefilter. The IR-hallucination risk is real; don't re-enable Haiku without the tightened prompt and the FORCED_OPUS_INTERVAL_SECONDS safety net described below.

**(Original section preserved below for context.)**


**Symptom:** Haiku 4.5 returned `novel_activity="false"` with reason "Female cardinal sitting on the nest" on a snap where the user manually verified there was NO cardinal in the image. The image was a nighttime IR view of an empty nest cup with rose bush foliage and some lighter straw shapes that pattern-matched as "bird" to Haiku. Opus 4.6 on the same image correctly returned `cardinal_on_nest="false"` with confidence 0.55.

**Why this is dangerous:** if Haiku falsely says "cardinal sitting" → drops the snap → no Opus call → state never updates → mother's actual absence is invisible to the system. Long-absence MEDIUM rule (15 min) gets delayed. Egg-count tracking misses fresh readings.

**Two-part mitigation (already implemented):**

1. **Tightened the prefilter system prompt** in `prefilter.py`. Now explicitly forbids Haiku from confabulating cardinal presence. Returns "uncertain" liberally on IR/low-contrast images. Quote from the prompt: *"DO NOT confabulate the cardinal's presence. If you cannot clearly distinguish her plumage and shape from the surrounding straw and foliage, return 'uncertain' — never 'false'."*

2. **Forced periodic Opus ground-truth calls** via new `FORCED_OPUS_INTERVAL_SECONDS` env var (default 300s = 5 min). Even if Haiku consistently says "no novel activity," `Pipeline.on_image()` forces an Opus call at minimum every N seconds. This bounds the worst-case "blind window" if Haiku is consistently wrong. Set to 0 to disable; raise to reduce cost.

**Verification:** After the fix, the same problematic image produced `novel_activity="uncertain"` from Haiku → escalation → Opus correctly identified empty nest. Documented in the dryrun output.

**Don't undo this without thinking carefully.** A future Claude might be tempted to "optimize" Haiku to be more decisive to save Opus calls. That's exactly the wrong direction — Haiku's job is to be humble about IR images. Cost savings come from `FORCED_OPUS_INTERVAL_SECONDS` (raise it) or `ACTIVE_HOURS` (narrow it), NOT from making Haiku overconfident.

### 9. Discord webhook returns HTTP 200, not 204, for multipart uploads

Discord docs say webhook execution returns 204 (No Content) on success. **That's only true for JSON-only `POST`s.** When you POST `multipart/form-data` with `payload_json` + `file`, Discord returns **HTTP 200 with the created message body in the response** (regardless of whether you set `?wait=true`). My initial `_post_with_retry` only treated 204 as success and logged 200 as an ERROR. Result: every feed snap (which uses multipart for the JPEG attachment) was logged as a Discord failure even though delivery worked perfectly.

**Fix in `notifier.py`:** treat both 200 and 204 as success. The actual message body in the 200 response confirms delivery (includes `id`, `channel_id`, the rendered embed, and `image.url` pointing to the Discord CDN where the attachment was hosted).

If you ever change retry/error logic in `_post_with_retry`, keep `if status in (200, 204): return True`. Don't drop 200.

### 10. Shutdown handler logs each step (after a fix on 2026-04-13)

The original `finally` block in `main.py` had `try: send offline embed; except: pass` which silently swallowed any failure. When the user reported missing offline embeds, we patched the handler to log each step (`tasks cancelled`, `sending offline embed`, `closing notifier session`, etc.) plus added a 10s timeout on the offline embed send so a stuck Discord call can't hang the shutdown forever.

If you ever modify shutdown sequence, **keep the per-step logging.** The user values being able to diagnose silent failures and explicitly asked for this visibility. Same applies to the feed notifier shutdown — it has its own log line.

### 12. Cadence race condition between snap_loop and on_image (fixed 2026-04-15)

`snap_loop` used to do `asyncio.create_task(on_image(jpeg, meta))` and then immediately call `get_interval()` to compute the next wait. This was a subtle race: `on_image` runs `await analyzer.analyze()` (3–6s) BEFORE it calls `state.record()` which flips `in_absence`. So `get_interval()` would read the PREVIOUS snap's state, always one iteration behind. Real-world symptom: when mom left at 11:54, cadence stayed at 300s until 12:00 — 5 minutes late.

**Fix:** replaced `asyncio.create_task(on_image(...))` with `await on_image(...)`. Now snap_loop waits for the full pipeline (analyzer + state.record + alert + feed enqueue) to finish before computing the next interval. Each cycle is ~4–10s longer; at 60–300s cadences this is negligible.

**Don't revert this** without a careful analysis. A future Claude might be tempted to "optimize" by restoring fire-and-forget for parallelism — the cost is that Pattern A's absence-aware cadence becomes off-by-one-iteration again, which is a life-critical defect for the eggs.

### 13. MEDIUM / HIGH rule tuning (2026-04-15, user-directed)

The user explicitly opted for aggressive alerting. Rules now:

| Rule | Before | After | Why |
|---|---|---|---|
| MEDIUM "long absence" threshold | 15 min | **5 min** | Mom's foraging trips are 5–15 min. User wants to know at 5 min, not 15. |
| MEDIUM cooldown | 15 min | **5 min** | MEDIUM repeats every 5 min while mom stays away. 10 min absence = 2 pings; 20 min absence = 4 pings. |
| HIGH "predator + absent" threshold | 2 min absence required | **no absence requirement** | Threat species + near_nest_activity fires HIGH immediately, mom present or not. User wants to intervene on ANY predator at the nest, not wait for mom to be absent. |
| HIGH cooldown | 5 min per species | 5 min per species (unchanged) | Prevents 1-alert-per-snap spam while a predator lingers. |

**Rationale the user gave verbatim:**
> "check the recent events to make sure we don't miss this scenario extrmemely important to get this right since it is life and death of birds/chicks"

**Don't revert these values to reduce alert noise** without the user's explicit direction. A silenced alert during a real predation event is far worse than a noisy-but-redundant MEDIUM.

### 14. Sonnet confuses female cardinal ↔ Brown Thrasher without explicit guidance (2026-04-15)

**Incident:** At 12:26 on 2026-04-15 the system fired a CRITICAL "direct nest interaction — Brown Thrasher" alert on an image that was actually the female cardinal. Opus-before-swap had made the same mistake earlier; Sonnet with the bare prompt reproduced it. Same week, a separate test on a real historical thrasher image came back classified as "Northern Cardinal (female)" at 0.62 confidence — a MISSED real threat.

Both errors had the same root cause: the original analyzer prompt didn't teach the model how to distinguish these two species, which genuinely look similar in this camera's backyard IR/daylight framing (both brownish, both small-ish). Without explicit feature guidance, Sonnet guessed — sometimes wrong in both directions.

**Fix applied:** rewrote `analyzer.py`'s `_SYSTEM_PROMPT` with explicit per-feature species-ID guidance:
- Female Cardinal: **RED/PINKISH CREST** (key feature, may be laid flat), tan/buff body with reddish wings, short ORANGE beak, dark face mask, ~21cm, short tail, dark eye.
- Brown Thrasher: **NO crest**, LONG thin tail (can equal body length), heavily STREAKED breast, YELLOW eye, long slightly curved beak, ~28cm, rich rusty-brown.
- Rule: if red crest visible → cardinal, NOT threat. If long tail + streaks + no crest → thrasher. If can't verify either → `threat_species_detected=["unknown"]` (don't guess "cardinal" by default).

**Also clarified `confidence` semantics:** confidence is about OBSERVATION RELIABILITY, not species-ID certainty. If Sonnet can clearly see a bird at the nest but can't tell the species, observation confidence should be HIGH (0.80+) and species should be "unknown" — this still fires a HIGH alert (the right outcome for ambiguous "something at nest"). Previously Sonnet was dropping confidence below 0.55 on ambiguous species and the whole observation was ignored.

**Also tightened `direct_nest_interaction` criteria:** only `true` when the bird's beak/body is UNAMBIGUOUSLY touching or reaching into the nest material. "Bird over the nest" is `near_nest_activity=true` (HIGH alert), not `direct_nest_interaction=true` (CRITICAL alert). This keeps CRITICAL for genuine predation attempts.

**Don't regress this.** The prompt section headed "== Species identification — READ CAREFULLY ==" is load-bearing. If a future Claude tries to shorten or "clean up" the prompt, they'll re-introduce the cardinal/thrasher confusion. Validate any prompt change via the regression test suite (next section).

### 16. Two-model verification on CRITICAL/HIGH alerts (added 2026-04-15)

After the false-CRITICAL incident (§14), we added a blind Opus second-opinion pass on any CRITICAL or HIGH alert before firing. The logic lives in `src/cardinal_nest_monitor/verifier.py`.

**Flow:**
```
Sonnet snap → evaluate() → decision
    ↓ (if decision.severity ∈ {CRITICAL, HIGH} AND settings.verify_alerts_with_opus)
analyzer.analyze(jpeg, model_override="claude-opus-4-6", extra_user_text=<nudge>)
    ↓
opus_obs → evaluate(opus_obs, same_pre_state) → opus_decision
    ↓
compute_verification_decision(sonnet_decision, opus_decision):
  • opus_decision is None (Opus saw no threat) → SUPPRESS the alert
  • opus_decision.severity.rank < sonnet.severity.rank → DOWNGRADE (use Opus's)
  • opus agrees or claims higher → FIRE Sonnet's decision (never upgrade)
  • Opus API failed → fall back to Sonnet's decision (fail open, don't lose real alerts)
```

**Anti-priming (CRITICAL — do not regress):** the Opus call is BLIND. It gets the same system prompt and the same image with NO hint of what Sonnet said. Priming it with "Sonnet thinks this is a thrasher, verify" would introduce anchoring bias and collapse the independent-second-opinion guarantee. The one allowed "nudge" is a generic "be especially careful with CRITICAL classifications" reminder — this does not mention Sonnet or any specific claim.

The `_VERIFICATION_NUDGE` constant in `verifier.py` is the exact text appended to the Opus user message. Read it before changing. It must NOT contain any hint of Sonnet's output.

**Scope:** only CRITICAL and HIGH. MEDIUM (long absence) and LOW (mother returned) are timing-based rules, not species-ID based; Opus verification wouldn't help them.

**Cost:** +$0.05 per verified alert. At ~5–20 alerts/day that's **+$7–30/month** on top of baseline ~$90.

**Latency:** +3–6s on CRITICAL/HIGH alerts while Opus re-analyzes. Acceptable given the overall reaction floor is 60–120s for motion events.

**Alert embed format:** when verification runs, the embed gets an extra `✓ Verification (claude-opus-4-6)` field showing Opus's confidence and summary. If Opus DISAGREED (suppressed), the alert doesn't fire at all — only a log line captures the suppression. Look for `Opus verification: ... → SUPPRESSED` in `~/Library/Logs/cardinal-nest-monitor/out.log` to see what Opus caught.

**Toggle:** `VERIFY_ALERTS_WITH_OPUS=false` in `.env` disables verification (falls back to one-pass alerts). Use sparingly — the verification is specifically there to prevent the false-CRITICAL trust-destruction mode from §14.

**Evidence:** when verification runs, the opus observation is saved to `evidence/.../verification.json` alongside `observation.json`. Useful for post-hoc review of disagreements.

### 15. Reference-image regression tests (added 2026-04-15)

Four real-world images live in `evidence/reference/`:

| Filename | What it is | Expected verdict (new prompt) |
|---|---|---|
| `historical_thrasher_1.jpg` | Thrasher over nest, seen from above/behind | `threat=unknown/brown_thrasher, near_nest=true, direct=false`, confidence ≥ 0.70 |
| `historical_thrasher_2.jpg` | Thrasher at nest, side view with wing streaks | Same as above |
| `historical_thrasher_3.jpg` | Ambiguous brownish bird at nest (face visible, no crest) | `threat=unknown, near_nest=true`, confidence ≥ 0.70 (must NOT classify as cardinal) |
| `2026-04-15/12-26-45_.../snap.jpg` | Female cardinal (caused the false CRITICAL incident) | `threat=unknown` is acceptable (HIGH false alarm); `threat=brown_thrasher` at high confidence or `direct_nest_interaction=true` is a REGRESSION (would re-fire false CRITICAL) |

**Regression harness (run after any prompt change):**

```bash
cd $PROJECT_ROOT
source venv/bin/activate
for img in evidence/reference/historical_thrasher_{1,2,3}.jpg; do
  echo "=== $img ==="
  python -m cardinal_nest_monitor.tools.dryrun --image "$img" --escalate
done
# Plus the false-positive image:
python -m cardinal_nest_monitor.tools.dryrun \
  --image evidence/2026-04-15/12-26-45_CRITICAL_brown_thrasher/snap.jpg --escalate
```

Accept a prompt change only if ALL of:
- All three thrasher images return a threat species (brown_thrasher or unknown) + near_nest_activity=true
- Cardinal FP image does NOT return `direct_nest_interaction=true` at confidence ≥ 0.55
- None of the thrasher images return `threat_species_detected=[]` (missed threat)

**User-collected future examples should be added here** — when real threats or ambiguous scenes show up in production, save the snap.jpg to `evidence/reference/` with a descriptive filename so the regression suite grows. Same for any false-positive cardinal images caught in operation.

### 11. Motion-event reaction floor is ~60–120s, NOT ~11–50s (corrected 2026-04-13)

**Earlier docs claimed ~11–50s reaction time on motion events. That was WRONG.** Here's why:

Blink does not expose a "motion in progress" signal to third parties. Our motion_loop polls `cam.recent_clips`, which only populates AFTER the clip has been uploaded to Blink's cloud. The actual sequence is:

```
t=0       PIR fires (instantly)
t=0–30s   Camera records (clip duration is configurable in the Blink app, default 30s, max 60s)
t=+5–30s  Sync Module uploads to Blink cloud
t=~60s    Clip appears in cam.recent_clips (first time we can see it)
t=+0–15s  Our motion_loop polls and detects the new entry
t=+6–8s   cam.snap_picture() → fresh JPEG
t=+5–10s  Haiku → Opus → Discord
══════════════
TOTAL: ~60–120s motion → Discord alert
```

**Why we still call snap_picture() instead of analyzing the clip:** the clip frames show what was happening 30–60s ago when motion fired. The fresh snap shows the CURRENT state — useful for "is the predator still there?" determinations. So snap_picture isn't FASTER than the clip availability; it's complementary (gives a now-state in addition to the historical clip).

**Implication for the architecture's design intent:** the scheduled snap loop is doing more heavy lifting than originally framed. Scheduled snaps catch:
- Slow / lingering threats (predator hangs around at the nest)
- Stealth threats that don't fire PIR (snakes, motionless predators)
- Mom presence/absence baseline tracking

Motion-triggered snaps catch fast threats but with a real ~1–2 minute reaction floor. Don't oversell the reaction speed to the user.

**TODO (deferred to next session):** add MP4 frame analysis as a secondary path so we ALSO analyze frames from the actual motion clip. This would catch fast in-and-out threats that snap_picture misses (because by the time the snap fires, the predator has left). Implementation: when on_clip downloads the MP4, extract 2-3 frames via cv2 and run them through the analyzer as a separate "post-hoc verification" pass. If the post-hoc pass disagrees with the snap-based decision (e.g., snap saw nothing but clip frame shows a thrasher), post a follow-up Discord alert.

### 17. Pipeline isolation is non-negotiable (2026-04-15 EMERGENCY)

**Incident:** On 2026-04-15 the service hung at **13:26:21 EST** and did not process another snap for **3+ hours**. `launchctl` reported the process as `running` but it was pinned at 0% CPU — a classic silent async deadlock. The user noticed the dead feed at **16:30 EST**; Discord had gone completely quiet during peak daylight predation hours. This is the worst failure mode the system has ever exhibited: an unnoticed, multi-hour monitoring blackout on a nest with live eggs.

**Root cause:** earlier that day I "fixed" the cadence race (see §12) by replacing `asyncio.create_task(on_image(...))` with `await on_image(...)` in `blink_client.py::snap_loop`. That change serialized the entire pipeline onto the snap loop. When Anthropic started returning `529 Overloaded` errors and one analyzer call hung without ever timing out, `snap_loop` hung with it — no more snaps, no more feed posts, no more alerts, no watchdog, nothing.

**The wrong pattern (what I had):**
```python
# DO NOT do this — one hung network call freezes the whole monitor
await on_image(jpeg, meta)
interval = get_interval()
```

**The correct pattern (restored + hardened):**
```python
state_updated = asyncio.Event()
asyncio.create_task(on_image(jpeg, meta, state_updated))
try:
    await asyncio.wait_for(state_updated.wait(), timeout=10)
except asyncio.TimeoutError:
    log.warning("on_image slow; cadence will use possibly-stale state this cycle")
interval = get_interval()
```

Per-snap `create_task` gives each pipeline run its own isolated task — a hung analyzer, Discord stall, or SQLite latency in one snap cannot cascade into freezing the next snap. The `state_updated: asyncio.Event` passed into `on_image` gets `.set()` right after `store.record()` lands, so the next cadence computation waits up to 10s for fresh state (preserving the Pattern A absence-aware cadence fix from §12) but never blocks indefinitely if the pipeline stalls.

**Don't revert this.** A future Claude reading §12 might be tempted to re-introduce `await on_image(...)` for "simpler code" or "correct cadence every cycle." That is the exact bug we just spent 3 hours and one real-world monitoring blackout recovering from. The cadence race (§12) and the hang cascade (§17) are solved together by `create_task` + `state_updated` event — not by choosing one over the other. Any change to `snap_loop`'s task-dispatch pattern MUST preserve per-snap task isolation. The same isolation principle is why §16's verification runs in a bounded way with `asyncio.wait_for` around Opus — one model being slow or down can never block the rest of the pipeline. If you find yourself writing `await some_network_call(...)` anywhere on the hot path without a timeout, stop and reach for `asyncio.wait_for` (see §19).

### 18. Integration test suite is mandatory (2026-04-15)

After the 3-hour outage, the user mandated a full integration test suite that must pass before any future deploy. The suite lives in `tests/integration/` and exercises the pipeline end-to-end against mocked analyzers + a **dedicated test Discord webhook** (with `[TEST]` prefixes so the user can distinguish test messages from real alerts).

**Directory:** `tests/integration/`

**Scenarios covered (13 tests):**
1. `test_normal_snap_no_alert` — mom on nest, no alert fires, feed post only.
2. `test_mother_leaves_cadence_tightens` — cadence flips from 300s → 60s when `in_absence` flips.
3. `test_medium_alert_at_5min` — MEDIUM fires at 5 min absence, repeats on the cooldown boundary.
4. `test_mother_returns_low_alert` — LOW "mother returned" fires, cadence returns to 300s.
5. `test_high_alert_thrasher_at_nest` — HIGH fires on threat + near_nest, verification runs, both verdicts land in the embed.
6. `test_critical_direct_interaction` — CRITICAL fires on `direct_nest_interaction=true`.
7. `test_verification_suppresses_false_critical` — Sonnet says CRITICAL, mock Opus says cardinal → SUPPRESSED, no Discord post.
8. `test_verification_downgrades_severity` — Sonnet CRITICAL, mock Opus HIGH → alert fires at HIGH.
9. `test_analyzer_timeout_does_not_hang` — mock analyzer sleeps 120s → `asyncio.TimeoutError` → pipeline continues.
10. `test_hung_on_image_does_not_block_next_snap` — one on_image hangs indefinitely → next snap still processes (the §17 regression guard).
11. `test_analytics_report_posts_to_discord` — `compute_report` + `send_analytics_report` → Discord embed with `[TEST]` prefix.
12. `test_feed_channel_single_tier_embed` — feed post has correct title/color/body for Sonnet-only.
13. `test_discord_failure_does_not_block_state_update` — Discord 500 error → state still persists, log-only.

**Test-mode flag:** `config.py` gets a new `test_mode: bool = Field(False)` and the env var `TEST_MODE=true` enables it. When active, `notifier.send_*` prefixes titles with `[TEST] ` and adds a footer line with the run timestamp so test messages are visually distinct in Discord. Production behavior is unchanged when `TEST_MODE` is unset (default `False`).

**Dedicated test Discord channel (2026-04-15):** All integration-test Discord posts route to `DISCORD_TEST_WEBHOOK_URL` (in `.env`) — a standalone channel the user created specifically for `[TEST]` traffic. The autouse `enable_test_mode` fixture in `tests/integration/conftest.py` rewrites `discord_webhook_url`, `discord_feed_webhook_url`, AND `discord_analytics_webhook_url` to point at the test webhook for the duration of every test. This keeps the three live production channels (alerts / feed / analytics) completely clean during test runs — every `[TEST]` message lands in the one dedicated channel where the user can review them without them bleeding into real alert traffic. `DISCORD_TEST_WEBHOOK_URL` must be set in `.env` or the autouse fixture hard-fails with a clear error. **Never route integration-test posts to the production channels.** If a future Claude deletes the test webhook or the redirect, the `[TEST]` messages will flood the real channels — this is forbidden.

**How to run:**
```bash
# From repo root:
source venv/bin/activate
TEST_MODE=true python -m pytest tests/ tests/integration/ -v
```

**CRITICAL RULE — load-bearing:** Before committing any change that touches `analyzer.py`, `events.py`, `main.py`, `blink_client.py`, `notifier.py`, or `verifier.py`, run `python -m pytest tests/ tests/integration/ -v`. If any test fails, **do not deploy**. Fix the test or revert. This is not a suggestion — it is a direct response to the 2026-04-15 outage. No exceptions, no "I'll run it next time," no "the change is small." The integration suite exists precisely to catch the kind of subtle async deadlock that silent-failed for 3 hours.

**Don't weaken this.** A future Claude might be tempted to skip integration tests for "obvious" changes or "just a refactor." That is exactly the path that produced the 2026-04-15 outage. If a test feels painful to run, fix the test or fix the harness — don't skip. If a test is legitimately obsolete, delete it in a dedicated commit with a written justification in the commit body.

### 19. Network-call timeouts are mandatory (2026-04-15)

Every external I/O call in the hot path MUST be wrapped in `asyncio.wait_for(..., timeout=N)` with an explicit, documented budget. "Network I/O" here means: Blink API, Anthropic API, Discord webhooks, and any other HTTP/socket-level call. Unbounded awaits are banned on the hot path — the 2026-04-15 outage was one unbounded `analyzer.analyze()` call hanging forever.

**Timeout budget table (from the plan's Part 1 C — this is the canonical reference):**

| Call | Timeout | Notes |
|---|---|---|
| `cam.snap_picture()` | 30s | normal 3–8s; headroom for Blink API stalls |
| `blink.refresh()` | 30s | normal <5s; poll API occasionally slow |
| `cam.get_media()` | 30s | normal <3s |
| `analyzer.analyze()` | 60s | normal 2–6s; Anthropic can take longer under load |
| `verifier.verify_alert()` | 60s | overall, on top of analyzer's internal 60s |
| `notifier.send_alert()` | 15s | Discord usually <1s; 15s for edge cases |
| `download_clip()` | 60s | video bytes; not hot path but still bounded |

**Rule (load-bearing):** Any NEW external I/O call must have an explicit `asyncio.wait_for` timeout. Budget should be roughly **typical p99 latency × 3**. When adding a new call, add a row to the table above and the code literal in the same commit. If you don't know the p99, pick a conservative 30s and revise once you've measured.

**Watchdog task (dead-man's-switch):** `main.py` runs a watchdog task that:
- Ticks every **60s**.
- Checks `pipeline._last_successful_snap_ts` (updated by `on_image` on every successful completion).
- If no snap has processed for **> 15 min during active hours**, posts a 🚨 "WATCHDOG: no snaps for 15+ min" embed to the urgent Discord channel and logs at ERROR level.
- Purpose: catch any future hang within 15 minutes, not 3 hours. If the hot path deadlocks again, the watchdog is the layer that tells us — even if every other notification path is hung.

**Don't regress this.** A future Claude might look at the watchdog and think "this never fires, it's dead code — remove it." That is a dead-man's-switch by design; its value is being there when something deeper breaks. Keep the watchdog, keep the timeouts, and when adding any new network call extend the timeout table above with the new row. If a timeout fires unexpectedly in production, investigate the underlying latency — do not just raise the budget to silence the alert.

### 20. Two-service architecture: decoupled downloader + analyzer (2026-04-15)

**Incident chain:** the 2026-04-15 3-hour outage (§17) → per-snap task isolation fix → realization that even a clean `launchctl kickstart -k` restart drops ~80s of snaps while the service re-initializes. That 80s window is exactly when a fast-moving predator event can be missed. Solution: decouple the Blink snap downloader from the analyzer so analyzer redeploys never interrupt image capture.

**Architecture:**

| Service | launchd label | `--role` | What it does | What it does NOT do |
|---|---|---|---|---|
| Downloader | `com.cardinalnest.downloader` | `downloader` | Blink API → raw JPEG → `data/spool/pending/` | No Anthropic calls, no Discord alerts, no state writes |
| Analyzer | `com.cardinalnest.analyzer` | `analyzer` | Polls spool → `Pipeline.on_image` → Discord alerts/feed/analytics | No Blink API access, no snap capture |
| Combined (dev) | `com.cardinalnest.monitor` | `combined` | Both loops in one process (legacy plist, byte-identical to pre-decouple) | N/A — this is the dev/smoke-test path |

**Spool protocol (`data/spool/`):**
- Downloader writes `{ts}_snap.jpg` + `{ts}_meta.json` to `pending/` via atomic rename (tmp-file + fsync + `os.rename`). Analyzer never sees half-written files.
- Analyzer claims newest-first by `os.rename()` from `pending/` to `processing/`. After `Pipeline.on_image` completes, deletes the pair from `processing/`.
- **Crash recovery:** on startup, analyzer moves anything stranded in `processing/` back to `pending/` (those were mid-flight when it died).
- **Cost cap:** snaps older than `BACKFILL_MAX_AGE_SECONDS` (default 1800 = 30 min) are silently dropped on startup. Prevents a multi-hour outage from burning $15+ of Anthropic spend in a recovery burst.
- **No separate tracking state needed.** The spool IS the resume mechanism: whatever's in `pending/` is unprocessed; whatever's in `processing/` was mid-flight and gets retried.

**Live vs. backfill routing:**

| Snap age | Classification | Discord channel | Alert title prefix |
|---|---|---|---|
| ≤ 30s | LIVE | `#alerts` (urgent) | (none — normal alert) |
| 30s – 30 min | BACKFILL | `#nest-backfill` | `[BACKFILL +Nm]` |
| > 30 min | DROPPED | (none) | (silently deleted, logged) |

The `[BACKFILL]` prefix and separate webhook (`DISCORD_BACKFILL_WEBHOOK_URL`) keep the urgent channel reserved for live, actionable events. Backfill embeds are visually identical otherwise (same severity colors, same fields, same image attachment). **Never watch `#nest-backfill` for real-time threats** — by definition, every embed there is stale.

**Cadence coordination (cross-process):** the downloader reads `state.sqlite` **read-only** via SQLite WAL mode (set in `StateStore.__init__`) to get `in_absence` for Pattern A dynamic cadence. If state hasn't been updated in >10 min (analyzer down), downloader falls back to a safe 60s constant. WAL mode is set by `PRAGMA journal_mode=WAL` on every connection — if this pragma is ever removed, cross-process reads will deadlock. **Do not remove the WAL pragma from state.py.**

**Redeploy playbook (the whole point of the split):**

```bash
# Restart ONLY the analyzer (downloader keeps capturing → zero snap gap):
launchctl kickstart -k gui/$(id -u)/com.cardinalnest.analyzer

# Restart ONLY the downloader (rare — only when blink_client.py changes):
launchctl kickstart -k gui/$(id -u)/com.cardinalnest.downloader

# Restart BOTH (when shared code like config.py or state.py changes):
launchctl kickstart -k gui/$(id -u)/com.cardinalnest.downloader
launchctl kickstart -k gui/$(id -u)/com.cardinalnest.analyzer

# Tail logs (separate files per service):
tail -F ~/Library/Logs/cardinal-nest-monitor/downloader.out.log
tail -F ~/Library/Logs/cardinal-nest-monitor/analyzer.out.log

# Rollback to single-process (if the split has issues):
launchctl bootout gui/$(id -u)/com.cardinalnest.downloader
launchctl bootout gui/$(id -u)/com.cardinalnest.analyzer
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ (edit paths in the plist to match your system)com.cardinalnest.monitor.plist
```

**Git tags for rollback:**
- `pre-decouple-v1` — known-good single-service architecture, all 63 original tests green. Full rollback point.
- `post-decouple-phase1` — code + tests for the decoupled architecture, 83 tests green. Combined mode is byte-identical to pre-decouple.

**Files added:**
- `src/cardinal_nest_monitor/spool.py` — atomic-rename file queue primitives.
- `src/cardinal_nest_monitor/downloader_loop.py` — Blink → spool producer, own watchdog + lifecycle embeds.
- `src/cardinal_nest_monitor/analyzer_loop.py` — spool → Pipeline consumer, live/backfill routing, runs all analyzer-side schedulers.
- `launchd/com.cardinalnest.{downloader,analyzer}.plist` — two LaunchAgent plists with separate log files.
- `tests/test_spool_unit.py` (11 tests), `tests/integration/test_spool_lifecycle.py` (5 tests), `tests/integration/test_backfill_routing.py` (4 tests).

**Don't regress this.** A future Claude might be tempted to merge the two services back into one "for simplicity." That is the exact path that produced the 2026-04-15 outage cascade. The decoupled architecture exists so that the volatile analyzer code (Anthropic calls, Discord webhooks, rules engine, verifier) can be iterated on without ever interrupting the stable downloader (Blink API + disk writes). If you need to change both, restart both — but the default deploy path should only touch the analyzer. The downloader changes maybe once a quarter; the analyzer changes weekly.

### 21. Burst cadence after absence (2026-04-16)

A thrasher attack takes ~4 seconds. The original 60-second absence cadence could miss a full raid. Burst cadence tightens the interval to 30 seconds for the first 3 minutes after `in_absence` flips True.

**How it works:** `StateStore.record()` sets `state.absence_started_ts = ts` when in_absence transitions False→True (and clears it on True→False). The downloader's `get_interval()` callback reads both `in_absence` AND `absence_started_ts`. If the snap is within `burst_duration_seconds` (180s default) of `absence_started_ts`, it uses `burst_snap_interval_seconds` (30s default). After the burst window expires, it falls back to `absence_snap_interval_seconds` (60s). Quiet hours always win.

**Foraging trip timeline:**

```
Before:   leave ── 60s ── 60s ── 60s ── 60s ── 60s ── 60s ── return
After:    leave ─30s─30s─30s─30s─30s─30s ── 60s ── 60s ── 60s ── return
          │──── burst (first 3 min) ────│──── normal absence ────│
```

First three minutes: 6 snaps instead of 3. After that, relaxes to the normal absence cadence.

**Config**: `BURST_SNAP_INTERVAL_SECONDS=30`, `BURST_DURATION_SECONDS=180`. Both in `.env.example`.

**Precedence in get_interval()**: quiet_hours > burst (if in_absence and within burst window) > absence > default.

**Cost impact**: a typical foraging trip (~5-15 min) now gets ~6 snaps in the first 3 min (instead of 3), then relaxes. Worst case ~2x snaps during absence windows, but absence windows are only ~5% of the day, so overall cost impact is small.

**Don't regress.** If you see `burst_snap_interval_seconds` removed or clamped higher than 60s, reconsider — the whole point is catching the 4-second raid window.

### 22. Multi-image analysis (2026-04-16)

Previously the analyzer received one downscaled JPEG per snap. Now it receives THREE crops per request:
- **Full frame** (~1024px) — context
- **Center crop** (middle 60%, up to 1280px at quality 90) — detail on the nest area
- **Overview** (~512px) — whole-scene context

Anthropic supports multi-image requests natively. The three crops let the model see both fine detail and context in a single inference call. Recall on subtle thrasher features improves (per Codex review: "what image(s) the model sees matters as much as the model name").

**Implementation**: `_image.prepare_multi_image(jpeg)` returns a list of 3 content blocks. `analyzer.analyze()` prepends a caption text block, then all three images, then the optional `extra_user_text` (verifier nudge). When `MULTI_IMAGE_ANALYSIS=false`, falls back to the single-image path.

**Cost**: ~2-3x input tokens per snap. At ~285 snaps/day: ~$180-270/mo (vs ~$90/mo baseline). Toggle with `MULTI_IMAGE_ANALYSIS=false` if spend becomes an issue.

**Opus 4.7 as verifier**: upgraded from 4.6 to 4.7 same day. Better vision, new high-resolution image support. Especially helpful on the detail-heavy center-crop variant.

### 23. Lifecycle tracking (2026-04-16, default ON)

Automatic detection of egg hatch → feeding → fledge transitions. The problem it solves: without this, the system keeps reporting "cardinal not on nest" as absence alarms when mom is actually off foraging for chicks. Cardinals start feeding chicks within hours of hatch and the absence pattern changes completely.

**Default ON via `LIFECYCLE_TRACKING_ENABLED=true`.** Set to `false` in `.env` as an escape hatch if a false positive fires — no code deploy needed, just restart the analyzer. The 2-sighting confirmation guard (below) makes false hatches very unlikely but the flag remains available.

**2-sighting confirmation guard (load-bearing):** a single chick sighting does NOT transition the stage. The 1st confirming signal (`chicks_visible="true"` OR `mother_feeding_chicks=true`) sets `state.first_chick_sighting_ts`. A 2nd confirming signal within 4 hours triggers the transition and fires the 🐣 alert. A 2nd signal OUTSIDE the 4-hour window is treated as a fresh "1st sighting" (the prior one was stale). This protects against a one-off analyzer misread triggering a false 🐣 alert. Cost: hatch detection is delayed by one snap interval (~30s-1min during burst, ~5min during normal cadence) — acceptable since the operational benefit is "stop firing false MEDIUMs during feeding" not "fire 🐣 within milliseconds of first chick sighting."

**Dedicated Discord channel**: 🥚/🪺/🐣/🦅 events all route to `DISCORD_LIFECYCLE_WEBHOOK_URL` (if configured) instead of the urgent alerts channel. The notifier rule_id allowlist for this routing is in `notifier.py::send_alert` and currently includes `egg_laying_begin`, `incubation_begin`, `hatch`, and `fledge` — extend this list when adding new lifecycle alerts. Keeps the urgent channel focused on threats. Backfill routing takes precedence over lifecycle routing (a stale backfilled hatch alert gets `[BACKFILL +Nm]` tag on the backfill channel rather than appearing as live on the lifecycle channel).

**Daily heartbeat embed shows lifecycle state.** When `LIFECYCLE_TRACKING_ENABLED=true`, the noon heartbeat embed in `notifier.send_heartbeat` includes a "Lifecycle" field with the current stage and a day counter — e.g. `Egg Laying · Day 2 of ~4`, `Incubation · Day 5 of ~12`, `Feeding · Day 7 of ~14`. Day labels come from `main._lifecycle_day_label(state)`, which keys off `egg_laying_started_ts` / `incubation_started_ts` / `hatch_detected_ts` / `fledge_detected_ts`. Stages without a canonical countdown (`building_nest`, `empty`) render the stage name only. The label gracefully drops out if lifecycle tracking is disabled.

**Detection approach — camera angle constrained:**

The Blink Outdoor camera is mounted on siding pointing at the rose bush at eye-level. It sees the mother's back/side when she's on the nest. It does NOT see inside the cup directly. So:

- We cannot detect hatch at the moment it happens (eggs are under mom)
- We detect hatch when chicks first stretch up above the cup rim for food (day 1-3 post-hatch)
- Feeding is detected when the cardinal is at the nest with a visible food item in her beak
- Fledge is detected by absence: no cardinal visits for 12+ hours after chicks confirmed, with no threat event in the prior 48 hours

**State machine (auto-transitions in `state.py::record()`):**

```
building_nest → egg_laying → incubation → feeding → fledging → empty
```

- `building_nest → egg_laying`: first confident `cardinal_on_nest=true`
- `egg_laying → incubation`: ≥70% `cardinal_on_nest=true` ratio over a 24h rolling window (min 24 confident samples)
- `incubation → feeding`: `chicks_visible="true"` OR `mother_feeding_chicks=true` (2-sighting confirmed)
- `feeding → fledging`: 12+ hours with no cardinal visits AND no threat in 48h
- `fledging → empty`: 72 hours of no activity

All transitions are one-way. Once we've moved forward, we don't go back.

**Egg-laying stage (user's real experience):**

When the system was first built, the cardinal was not on the nest at night. The camera showed an empty cup, hour after hour. The user panicked — convinced the mother had abandoned the eggs.

Female cardinals lay one egg per day over 3–4 days. During this laying phase they do not incubate — they visit briefly to lay, then leave. Full incubation only begins AFTER the last egg is down.

The system's old 4-stage model didn't represent this; it fired MEDIUM "absence" alerts all night during laying. Adding `egg_laying` as a first-class state fixes this: during `egg_laying`, "mom is gone for hours overnight" is expected behavior, NOT an alarm. The MEDIUM long_absence rule checks `lifecycle_stage` and suppresses in `egg_laying`.

**Auto-transition: `building_nest → egg_laying`:**

Trigger: first confident `cardinal_on_nest=true` observation. Fires a LOW alert `egg_laying_begin` with title "🥚 Egg laying has begun" to the lifecycle Discord channel.

Default stage for brand-new deployments is still `incubation` (not `building_nest`) — most users deploy AFTER discovering an existing nest. Manual operators who deploy during nest-building can set `lifecycle_stage='building_nest'` via the backfill tool.

**Auto-transition: `egg_laying → incubation`:**

Trigger: ≥70% `cardinal_on_nest=true` ratio over any 24h rolling window of confident observations (minimum 24 samples).

Why 70% and not 95% (which would match true incubation behavior): IR night suppression, quiet-hours cadence, and analyzer uncertainty all push the signal down. 70% is the empirical boundary between "visits to lay" and "sustained sitting."

Logic lives in BOTH `state.py::record()` (performs the state write) AND `events.py::_lifecycle_event` (predicts the transition to fire the alert on the same snap). They must stay in sync — if you change the threshold or sample-count rule in one, change it in the other in the same commit.

Fires a LOW alert `incubation_begin` with title "🪺 Incubation has begun" to the lifecycle Discord channel.

**Backfill tool: `tools/lifecycle_backfill.py`:**

Usage:
```bash
python -m cardinal_nest_monitor.tools.lifecycle_backfill --auto
# or manual:
python -m cardinal_nest_monitor.tools.lifecycle_backfill \
  --incubation-started 2026-04-15 --egg-laying-started 2026-04-11
```

Idempotent; refuses to overwrite set values without `--force`. For the current monitored brood, the user ran `--auto` to populate `egg_laying_started_ts` and `incubation_started_ts` from observation history (cardinal was already past building_nest when monitoring began on 2026-04-13; transitioned to incubation ~2026-04-15).

**Events fired:**

- 🐣 `hatch` (LOW alert, green) — first confirmed chick observation
- 🦅 `fledge` (LOW alert, green) — chicks have left
- MEDIUM long_absence is suppressed for 30 min after any feeding event (mom is expected to cluster feeding trips)

**Schema additions:**

- `NestObservation.chicks_visible` (Tristate), `chick_count_estimate` (int|None), `mother_feeding_chicks` (bool) — new fields returned by the analyzer
- `NestState.lifecycle_stage` (str; Literal now includes `building_nest` and `egg_laying` alongside `incubation`/`feeding`/`fledging`/`empty`), `last_chick_count`, `hatch_detected_ts`, `fledge_detected_ts`, `last_feeding_event_ts`, `egg_laying_started_ts`, `incubation_started_ts` — new tracked columns
- Idempotent SQLite `ALTER TABLE` migration runs on every startup

**Analyzer prompt extension:** new sections "CHICKS vs EGGS" and "Feeding behavior" teach Sonnet to distinguish chicks from eggs, recognize food in the beak, and use `chicks_visible="uncertain"` when mom is covering the cup.

**Real-image regression suite (hard gate before enabling):**

`evidence/reference/lifecycle/` has 13 curated Wikimedia Commons images covering incubation + chick stages, each with `.expected.json` ground truth. Before setting `LIFECYCLE_TRACKING_ENABLED=true`:

```bash
python -m cardinal_nest_monitor.tools.lifecycle_regression
# Must report: ALL PASS — safe to enable LIFECYCLE_TRACKING_ENABLED=true
```

If any image fails, the feature does not ship. The prompt needs tuning first. This is a **hard gate** — not optional.

**Cost of the regression run:** ~$0.30-0.50 per run (13 real Anthropic API calls). Run it before any prompt change that touches the chick/feeding sections.

**Test coverage (as of 2026-04-16):**
- `tests/test_lifecycle.py` — 15 unit tests covering state transitions, hatch/fledge alerts, feeding suppression, predation-during-feeding, flag-off regression guard
- `tests/integration/test_lifecycle_cycle.py` — 4 integration tests via Pipeline.on_image including Discord posts
- 120 total tests pass (101 baseline + 19 new)

**Don't regress this.** A future Claude might be tempted to:
- Merge the lifecycle transition logic into events.py (keep it in state.py::record() — that's where observation-driven state lives)
- Remove the 24h cooldown on hatch/fledge rules (prevents re-firing if state hiccups; load-bearing)
- Loosen the feeding-event suppression to "always suppress during feeding stage" (would miss genuine emergencies; keep the 30-min bounded window)
- Collapse the 6 stages back to 4 "for simplicity." The `egg_laying` stage exists specifically to suppress the multi-hour overnight absence storm that convinced the user the eggs had been abandoned during the first week of monitoring. Removing it re-creates that panic mode.

---

## Analytics channel (optional third Discord channel)

`DISCORD_ANALYTICS_WEBHOOK_URL` (in `.env`) enables a third Discord channel that receives aggregated behavior reports every `ANALYTICS_REPORT_HOURS` (default 8). Reports include: foraging trip count + durations, on-nest vs off-nest time, longest absence, threat sightings by species, alert counts by severity, and system health (snap count, estimated cost).

**Zero Anthropic spend** — pure SQLite aggregation from the `observations` + `alerts` tables.

**Isolation architecture (hard requirement):** the compute step (SQLite read + trip-detection loop) runs on a **dedicated single-worker ThreadPoolExecutor** (`_analytics_executor` in `main.py`), invoked via `loop.run_in_executor(...)`. This guarantees the main asyncio event loop — which serves the alert hot path and the snap-feed worker — is never blocked by analytics CPU work, regardless of how long the query takes. Priority order enforced:

| Priority | Path | Guarantee |
|---|---|---|
| 🥇 Urgent alerts | `Pipeline.on_image` → `notifier.send_alert` | Main event loop, runs synchronously after analyzer. Never blocked by analytics. |
| 🥈 Snap feed | `feed_worker` draining bounded queue | Main event loop, bounded queue drops rather than backs up. |
| 🥉 Analytics | `analytics_scheduler` → dedicated thread pool | CPU work entirely off main event loop. Webhook post is one HTTP per 8h. |

Other properties:
- Third independent `Notifier` instance; own `aiohttp.ClientSession`
- Catches all exceptions internally; bug in analytics can't affect alerts
- Shutdown order: analytics closes AFTER alert + feed notifiers, so a slow analytics shutdown can't delay the `🔴 offline` embed
- `max_workers=1` means multiple analytics runs never compound; sequential only

**Where the metrics live:** `src/cardinal_nest_monitor/analytics.py` has `compute_report(store, window_end_ts, window_hours, analyzer_model)` as the sole entry point. Trip detection walks confident `cardinal_on_nest` transitions (true→false = leave, false→true = return). Low-confidence and "uncertain" observations don't disturb transitions.

**Two schedulers run for the analytics channel (both use the same dedicated executor):**

| Scheduler | Cadence | Window | Purpose |
|---|---|---|---|
| `analytics_scheduler` | Every `ANALYTICS_REPORT_HOURS` (default 8h) from service start | Last 8h | Intraday checkpoints; drifts with service restarts |
| `daily_analytics_scheduler` | Wall-clock aligned to `ANALYTICS_DAILY_HOUR` (default 08:00 local) | Last 24h | Dependable morning briefing regardless of restart time |

Both produce the same embed shape. Set `ANALYTICS_DAILY_HOUR=-1` to disable the daily one.

**Tools:**
- `python -m cardinal_nest_monitor.tools.analytics_once [--hours N]` — fire a single report immediately (useful for smoke-testing or ad-hoc reports without waiting for the next scheduler tick).

**To disable:** unset `DISCORD_ANALYTICS_WEBHOOK_URL` in `.env` and restart launchd. The alert + feed channels are unaffected.

---

## Snap feed (optional second Discord channel)

`DISCORD_FEED_WEBHOOK_URL` (in `.env`) enables a parallel "every-snap" feed channel — receives one embed per snap with the JPEG attached and Claude's text reply. Designed for sharing visibility / watching the system reason in real time.

**Isolation guarantees:**
- Implemented as a bounded `asyncio.Queue(maxsize=100)` + dedicated `feed_worker` task.
- Hot path uses `put_nowait` — never awaits Discord; drops events with a warning if the queue is full.
- Worker catches all exceptions; a misbehaving feed webhook can't affect alerts.
- If env var is empty, no worker is started, no queue exists. Zero overhead.

**Embed shape per snap:**
- Title color depends on outcome:
  - 📷 grey — Haiku said "no novel activity," not escalated
  - 🔍 blue — escalated to Opus, no alert fired
  - 🚨/⚠️/🟡/✅ — alert severity color (when an alert also fires)
- Description: Tier-1 reason + Tier-2 summary if escalated
- Fields: trigger (scheduled vs motion event), local time
- Image: snap.jpg attached

**Discord webhook gotcha (already fixed, see §9):** Discord returns HTTP 200 (with the message body), not 204, for multipart uploads. `_post_with_retry` accepts both as success. Don't regress this.

**Volume at current cadence (5min day / 30min quiet):**
- ~250 posts/day per webhook (~2 posts/hour during quiet, ~12 posts/hour during day)
- Plus motion-triggered posts (variable; depends on activity)
- Discord rate limit is 30 msg/60s per webhook → easily within bounds.

**Alert webhook also includes Tier 1 + Tier 2 attribution (added 2026-04-13).** Every alert embed shows the prefilter (Haiku) verdict + reason AND the analyzer (Opus) summary so you can debug false-positive alerts. The user explicitly requested this after a false LOW alert at 60% Opus confidence — knowing which model said what makes triage easy.

**To disable feed:** unset/empty `DISCORD_FEED_WEBHOOK_URL` in `.env` and restart launchd. The alert webhook is unaffected.

---

## Operational reference card

```bash
# ── Check it's running (two-service mode, see §20) ───────────────
launchctl list | grep cardinalnest
# look for: both com.cardinalnest.downloader AND com.cardinalnest.analyzer
# with exit code 0 and a PID

# ── Tail live logs (separate per service) ─────────────────────────
tail -F ~/Library/Logs/cardinal-nest-monitor/downloader.out.log   # downloader
tail -F ~/Library/Logs/cardinal-nest-monitor/analyzer.out.log     # analyzer
# Legacy combined logs (when using --role=combined):
tail -F ~/Library/Logs/cardinal-nest-monitor/out.log

# ── Pause for battery swap ────────────────────────────────────────
cd $PROJECT_ROOT
source venv/bin/activate
python -m cardinal_nest_monitor.tools.pause 10           # pause 10 min
python -m cardinal_nest_monitor.tools.pause --clear      # resume now

# ── Install deps (lockfile, see §30) ──────────────────────────────
# Production / CI (exact-pin reproducibility — preferred for deploy):
pip install -r requirements.lock
# Dev (editable install with optional test extras):
pip install -e .[dev]
# Refresh the lockfile after a deliberate upgrade:
pip freeze --all > requirements.lock && git add requirements.lock

# ── Stop / start / restart (two-service mode) ────────────────────
# Analyzer-only restart (most common — code changes to analyzer/events/notifier):
launchctl kickstart -k gui/$(id -u)/com.cardinalnest.analyzer
# Downloader-only restart (rare — only when blink_client.py changes):
launchctl kickstart -k gui/$(id -u)/com.cardinalnest.downloader
# Full stop (both services):
launchctl bootout gui/$(id -u)/com.cardinalnest.downloader
launchctl bootout gui/$(id -u)/com.cardinalnest.analyzer
# Full start (both services):
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ (edit paths in the plist to match your system)com.cardinalnest.downloader.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ (edit paths in the plist to match your system)com.cardinalnest.analyzer.plist
# Rollback to single-process combined mode (see §20 for details):
# launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ (edit paths in the plist to match your system)com.cardinalnest.monitor.plist

# ── Smoke tests (foreground, no launchd) ─────────────────────────
source venv/bin/activate
python -m cardinal_nest_monitor.tools.test_discord                          # 🧪 embed test
python -m cardinal_nest_monitor.tools.dryrun --image evidence/SAMPLE.jpg    # pipeline against local JPEG
python -m cardinal_nest_monitor                                             # full system, foreground; Ctrl+C to stop

# ── Lifecycle backfill (one-shot, see §23) ────────────────────────
# Auto-infer egg_laying_started_ts + incubation_started_ts from observation
# history (looks for earliest 24h window with ≥70% sitting ratio):
python -m cardinal_nest_monitor.tools.lifecycle_backfill --dry-run --auto
python -m cardinal_nest_monitor.tools.lifecycle_backfill --auto
# Manual override (when --auto can't find a qualifying window):
python -m cardinal_nest_monitor.tools.lifecycle_backfill \
    --incubation-started 2026-04-14T00:00 \
    --egg-laying-started 2026-04-13
# Idempotent — re-runs without --force are no-ops.

# ── Re-auth Blink (token expired or stale) ───────────────────────
rm -f /tmp/cardinal_nest_blink_pin
python -m cardinal_nest_monitor --auth-only &
# wait for email, then:
echo "PIN_FROM_EMAIL" > /tmp/cardinal_nest_blink_pin

# ── Review recent events ──────────────────────────────────────────
ls evidence/$(date +%Y-%m-%d)/   # NONE_unk = quiet snap, anything else = alert fired
# Each event dir contains:
#   snap.jpg          the JPEG sent to Anthropic
#   prefilter.json    Haiku result
#   observation.json  Opus result (only if escalated)
#   meta.json         timing, decision, motion-triggered flag
#   clip.mp4          Blink MP4 (if motion event triggered async download)

# ── Inspect SQLite state ──────────────────────────────────────────
sqlite3 data/state.sqlite "SELECT * FROM state WHERE id = 1;"
sqlite3 data/state.sqlite "SELECT ts, severity, rule_id, species, title FROM alerts ORDER BY ts DESC LIMIT 10;"
sqlite3 data/state.sqlite "SELECT COUNT(*), DATE(ts, 'unixepoch', 'localtime') AS day FROM observations GROUP BY day;"

# ── Disk hygiene (evidence accumulates ~150 MB/day) ───────────────
find evidence -mtime +14 -delete     # delete event dirs older than 14 days
```

---

### 24. IR-mode suppression for false MEDIUM in the sunset→quiet-hours gap (2026-04-16)

**Incident:** on the evening of 2026-04-16 the urgent channel fired a MEDIUM "Mother away from nest for 30+ minutes" at 20:48 EDT. The cardinal was actually on the nest. Sonnet correctly returned `cardinal_on_nest="uncertain"` at 0.62 confidence with a summary that explicitly said *"A compact bird is settled low in the nest cup at night in IR mode … body posture and size are consistent with the incubating female cardinal, but the crest is not clearly visible and species cannot be confirmed."* Sonnet wasn't wrong — it was being appropriately cautious about grayscale ID. The rules engine was wrong.

**Root cause:** the Blink Outdoor camera switches to IR at sunset (~20:00 in April-Atlanta). Our `QUIET_HOURS` wall-clock window doesn't start until 23:00. That leaves a ~3-hour gap where IR is on, the cardinal is hard to ID in grayscale, and the old rule 4 suppression (clock-gated) doesn't apply. The absence counter keeps climbing from the last confident sighting (20:16 in this case), crosses the 5-min threshold, and MEDIUM fires.

**Fix:** new helper `events.observation_indicates_ir_mode(obs)` checks the analyzer's own summary text for phrases like *"IR mode"*, *"infrared"*, *"grayscale IR"*, *"night IR"*, *"night vision"*. The long_absence rule now suppresses on IR-detected in addition to clock-gated quiet hours. Matching IR-mode guard added to `state.py::record()` so the confidence floor for `in_absence` flips rises from 0.55 to 0.75 during IR mode too.

**Why text-based detection and not JPEG-grayscale detection:** Sonnet consistently mentions IR/grayscale when the image is in IR (observed across every 2026-04-16 evening snap). Text detection is free (no decode), reliable, and doesn't require a schema or prompt change. JPEG-grayscale detection would also work but costs more complexity for marginal robustness gains.

**Don't regress.** A future Claude tempted to "clean up" the `_IR_MODE_PHRASES` list should note: these are the phrases Sonnet actually produced in evidence/2026-04-16/20-*_MEDIUM_unknown_bird/observation.json. Removing a phrase re-opens the false MEDIUM window. If Sonnet starts using a new phrasing, ADD to the list — don't replace.

---

### 25. Correctness guardrails from the 2026-04-16 codex review

Three independent correctness bugs Codex flagged after the lifecycle expansion shipped. All four P1/P2 findings are fixed:

**a) Stale-snap protection in `state.py::record()` (Codex P1).**
The spool claims newest-first (`spool.claim_next`), so during analyzer recovery after downtime, older snaps can be processed AFTER newer ones. Without a guard, an old backfilled snap would overwrite the single-row derived state — rolling `in_absence` back, clearing `absence_started_ts`, regressing `lifecycle_stage` — until the next live snap lands. The downloader reads this state for cadence, so recovery could briefly run on stale truth.

Fix: at the top of `record()`, query `MAX(ts) FROM observations`. If the incoming `ts < latest`, insert the observation for history but SKIP the UPDATE state step entirely. Events.py still runs against pre-state and can fire backfill alerts through the normal `[BACKFILL +Nm]` routing.

Don't regress by "simplifying" to always-update. The stale-snap guard is why the downloader's cadence can trust `in_absence` even when an old snap lands in the middle of a burst window.

**b) `nest_visible` guard on lifecycle transitions (Codex P2).**
`events.py::_lifecycle_event` ran before the universal smart-filter gate, and `state.py::record()`'s lifecycle block didn't require a nest-visible frame either. A yard-motion or obscured frame where `cardinal_on_nest != "true"` could therefore emit a false `fledge` alert, and state could advance on a non-nest frame.

Fix: both paths now require `observation.nest_visible=True` before evaluating or transitioning. If the analyzer can't see the nest in this frame, that frame can't advance lifecycle state.

**c) Proper confidence filter in the 24h sitting-ratio scan (Codex P2).**
The scan documentation claimed "≥ 0.55 confidence" but the implementation was `'"confidence":' in oj` — a substring check that accepted every row because `model_dump_json()` always includes the field. Low-confidence IR misreads therefore contributed to the ratio and could trigger `egg_laying → incubation` prematurely. Same bug was in `tools/lifecycle_backfill.py`.

Fix: new `_row_passes_confidence(oj, floor=0.55)` in `state.py` parses the float via regex (`r'"confidence":([0-9]*\.?[0-9]+)'`). Both `state.py::record`, `events.py::_lifecycle_event`, and `lifecycle_backfill.py` share this helper.

Post-fix re-run of the backfill on the production DB returned the SAME window (2026-04-14 05:48, 74%, n=91) — the old substring check happened to coincide with the proper filter because every production observation already had confidence ≥ 0.55 naturally. No data correction needed, but the bug would have mattered under different conditions.

**d) Downloader 10s cadence wait (Codex P2).**
`snap_loop()` always waits up to 10s for `state_updated` after each snap (preserves Pattern A absence-aware cadence on the analyzer side), but downloader-role `on_snap` never set the event. Every downloader cycle burned the full 10s wait before computing the next interval — silently making real burst/absence cadence 10s slower than configured.

Fix: downloader `on_snap` now `.set()`s `state_updated` in a `finally` block (even on error). Downloader has nothing to wait for (no analyzer-side state), so setting immediately is correct.

**Testing.** 5 new regression tests in `tests/test_lifecycle.py`:
- `test_lifecycle_event_skips_frames_without_nest_visible`
- `test_lifecycle_transition_skipped_when_nest_not_visible`
- `test_low_confidence_rows_excluded_from_sitting_ratio`
- `test_stale_snap_inserted_but_does_not_touch_derived_state`
- `test_stale_snap_does_not_regress_lifecycle_stage`

140/140 tests pass post-fix. **Never regress the stale-snap guard or the nest_visible gate** — both protect against classes of silent state-rollback bugs that are easy to re-introduce during "simplification."

---

### 26. Backfill snaps must skip state-relative rules (Codex round 2, 2026-04-17)

**Incident:** even after §25 fixed `state.py::record()` to skip the derived-state UPDATE for stale snaps, Codex reproduced a related bug at the next layer up. `Pipeline.on_image()` was still calling `evaluate(obs, current_state, store, ts)` for every snap including backfill. State-relative rules (mother_returned, long_absence, egg_loss, lifecycle transitions) compared the stale snap's `ts` against state that reflects FUTURE truth, producing nonsense like a `mother_returned` alert with `absence_seconds=-300`.

**Fix:** `Pipeline.on_image` now detects stale snaps by comparing `ts` against `MAX(observations.ts)` BEFORE calling `evaluate()`, and passes `is_backfill=True`. In `events.py::evaluate`, when `is_backfill=True` we skip:
- `_lifecycle_event` (state-relative — would mis-fire egg_laying_begin / hatch / fledge)
- Rule 2 `egg_loss` (compares against state.last_known_egg_count which may have been set after this snap)
- Rule 4 `long_absence` (uses state.last_mother_seen_ts which may be future)
- Rule 5 `mother_returned` (uses state.in_absence which may be future)

We KEEP for backfill:
- Rule 1 `direct_attack` (CRITICAL — observation-only, signals "thrasher's beak in the cup at 14:32 during downtime")
- Rule 3 `predator_near_nest` (HIGH — observation-only, signals "predator at the nest during downtime")

These remain operationally valuable through the existing `[BACKFILL +Nm]` channel routing in `analyzer_loop.py` — they tell you what happened during analyzer downtime without polluting the live channel.

**Belt-and-suspenders:** rule 5 also got a defense-in-depth check `ts >= state.last_mother_seen_ts`. Even if a future caller forgets to set `is_backfill`, no negative-absence alert can fire.

**Don't regress.** The `is_backfill` plumbing in `Pipeline.on_image` is load-bearing — the stale check `MAX(observations.ts)` matches the same check inside `state.py::record()`'s stale-snap guard, so they fire on the SAME set of snaps. If a future Claude moves either check, they MUST move together. A snap that's "live" for state-write purposes must also be "live" for evaluate purposes, or vice versa — desync would produce silent contradictions (state stays current but alerts get suppressed, or vice versa).

5 new regression tests in `tests/test_events.py` cover this:
- `test_backfill_snap_does_not_fire_mother_returned_with_negative_absence` (Codex's exact repro)
- `test_backfill_snap_does_not_fire_long_absence`
- `test_backfill_snap_still_fires_direct_attack_threat`
- `test_backfill_snap_still_fires_predator_near_nest`
- `test_negative_absence_guard_in_mother_returned_belt_and_suspenders`

**Round-3 follow-up (analytics consistency, 2026-04-17):** `analytics.py::_trip_detection` and `_presence_totals` were still using only wall-clock `quiet_hours` to coerce ambiguous frames as on-nest. The live alert path's IR-mode suppression (§24) hadn't propagated. A dusk IR false-negative would NOT fire a MEDIUM alert (correct) but would still appear as a phantom foraging trip in the 8h analytics report (wrong). Fix: extracted a string-form `summary_indicates_ir_mode(summary)` helper from `events.py` and applied the same IR coercion in both analytics functions. Live alerts and analytics now agree on what IR frames mean. 3 new regression tests in `tests/test_analytics.py`:
- `test_dusk_ir_false_off_does_not_invent_trip`
- `test_dusk_ir_does_not_inflate_off_nest_seconds`
- `test_dusk_non_ir_off_frame_still_counts_as_off` (negative control)

If you ever extend the IR-phrase allowlist in `events.py::_IR_MODE_PHRASES`, you don't need to touch analytics — both live and analytics paths share `summary_indicates_ir_mode()`. **Don't fork these matchers.** Live and analytics disagreeing about IR detection would produce silent inconsistencies between Discord alerts and Discord reports.

**Round-4 follow-up (cooldown + verifier, 2026-04-17):** Codex reproduced two more backfill-only correctness gaps that survived rounds 1–3.

- *Future-blind cooldowns.* `state.py::cooldown_active` and `latest_alert_for_species` queried `MAX(ts) FROM alerts WHERE ...` then compared to the caller's `ts` in Python. They DIDN'T constrain `WHERE ts <= ref_ts`. So during newest-first backlog drain, a newer alert (recorded at 12:10) was returned when evaluating an older 12:00 snap, and the Python `(ref - row_ts) < window_s` check fired True for the negative difference — silently suppressing legitimate older historical alerts of the same species. Codex's exact repro: alert at `ts=2000`, `cooldown_active(... ts=1000)` returned True. Fix: both queries now include `AND ts <= ?` in the SQL. Also makes the eval timeline deterministic — cooldowns now answer the question "was there a prior alert in the window AS OF this snap's timestamp" rather than "as of the latest known".

- *Verifier dropped is_backfill.* `Pipeline.on_image` correctly passes `is_backfill=True` to `evaluate()` for stale snaps (round 2), but the verifier's internal `evaluate(opus_obs, pre_state, store, ts)` call dropped that context. Opus's verdict ran with full live-mode rules. Result: a stale snap that fires CRITICAL `direct_attack` from Sonnet could be downgraded or suppressed by a bogus Opus `mother_returned` / `long_absence` decision against future state. Fix: `verify_alert()` now takes `is_backfill: bool = False` and forwards it to its internal `evaluate()`. `Pipeline.on_image` passes the same flag it computed for the Sonnet pass.

5 new regression tests:
- `tests/test_cooldown.py::test_cooldown_active_ignores_future_alerts` (Codex's exact repro)
- `tests/test_cooldown.py::test_latest_alert_for_species_ignores_future_alerts`
- `tests/test_cooldown.py::test_cooldown_still_works_for_historical_alerts` (negative control)
- `tests/test_verifier.py::test_verify_alert_forwards_is_backfill_to_evaluate`
- `tests/test_verifier.py::test_verify_alert_default_is_backfill_false`

**Don't regress.** The pipeline's `is_backfill` flag MUST flow into both `evaluate()` AND `verify_alert()` for every stale snap; cooldown queries MUST include `WHERE ts <= ref_ts`. These three checks are coupled — a stale snap should see the same set of "as-of" history in evaluate, in verify, and in cooldown lookups, or backfill behavior diverges silently across paths. If you find yourself adding a new alert-history query, it MUST take a `ts` parameter and constrain SQL to `ts <= that`.

**Round-5 follow-up (rule-scoped cooldowns, 2026-04-17):** Codex reproduced a real over-suppression bug AND surfaced an adjacent dormant bug:

- *mother_returned over-suppressed by unrelated LOW alerts.* Rule 5 called `cooldown_active(Severity.LOW, None, _MOTHER_RETURN_COOLDOWN, ts)` — that checks the most recent LOW alert of *any* kind. So a celebratory lifecycle LOW (hatch, fledge, egg_laying_begin, incubation_begin) silently suppressed real "mom is back" alerts for 5 minutes. Codex repro: a LOW hatch alert 10s before a valid return frame returned None.
- *Lifecycle cooldowns were silently dead.* The four lifecycle-alert sites used `_cooldown_blocks(store, sev, "hatch", ...)` (etc.) which calls `latest_alert_for_species(species, ...)` with the rule_id passed as the species argument. Lifecycle alerts have empty species lists → the species column is NULL → no row ever matched → cooldown was effectively never active. The one-way state-machine transitions in `state.py::record()` were the only thing preventing double-fires. Today's behavior is correct because of that, but the cooldown was load-bearing only on paper — any change to the state machine that re-allowed re-entry would have produced alert spam.

Fix: new `state.rule_cooldown_active(rule_id, window_s, ts)` that queries `WHERE rule_id = ? AND ts <= ?`. Switched all 5 sites to it: rule 5 mother_returned + the 4 lifecycle alerts. Rule-scoped cooldowns are now actually rule-scoped, and the dormant bug becomes a real working belt-and-suspenders.

3 new regression tests in `tests/test_cooldown.py`:
- `test_lifecycle_low_does_not_silence_mother_returned` (Codex's exact repro)
- `test_mother_returned_self_cooldown_still_works` (negative control: same rule still suppressed within window)
- `test_rule_cooldown_active_basic` (direct helper test: same rule blocked, different rule not blocked, future alert ignored)

**Don't go back to severity-scoped cooldowns** for rule-specific alerts. The general principle: if the cooldown's intent is "don't re-fire THIS rule too often", scope to rule_id. If it's "don't re-fire ANY alert of this severity for this species too often" (the original Brown-Thrasher-spam guard for rules 1-3), keep severity+species scoping. The two patterns coexist in `state.py` and serve different bugs.

---

### 27. Cost estimate now accounts for multi-image + verifier (2026-04-17)

`DailyCounters.estimated_cost` and `analytics._system_health` were both pegged at a flat `$0.01`/snap, which was correct in the pre-multi-image era. With `MULTI_IMAGE_ANALYSIS=true` (default since 2026-04-16) per-snap cost is roughly `$0.02` — and the verifier's blind Opus second-opinion adds ~`$0.05` per CRITICAL/HIGH. Heartbeat and analytics reports were materially undercounting real spend.

Fix:
- `_ANALYZER_COST_PER_CALL` bumped 0.01 → 0.02 in `main.py` and `analytics.py`.
- `DailyCounters` now tracks `verifier_calls` separately; `Pipeline.on_image` increments the counter when the verifier path runs.
- `_system_health` in `analytics.py` estimates verifier cost from CRITICAL/HIGH alerts in the window (slight over-count when `verify_alerts_with_opus=False`, accurate otherwise).
- New `verifier_calls` field in the analytics report dict.

These are estimates — not metered billing. For ground truth, check the Anthropic console at the end of the month.

---

### 28. Watch-items (intentionally not yet fixed)

Things Codex called out as "workable" or "low priority" that we ack and watch but don't preemptively fix:

**a) IR-mode detection keys off Sonnet's free-form summary text** (§24).
The `_IR_MODE_PHRASES` allowlist in `events.py::observation_indicates_ir_mode` matches phrases like *"IR mode"*, *"infrared"*, *"grayscale IR"*. If Sonnet's prompt or model wording drifts (Anthropic ships a new model variant that says *"low-light view"* instead of *"IR mode"*), the suppression silently degrades and the dusk false-MEDIUM window re-opens. Mitigation: when Sonnet's wording changes (e.g. after an Anthropic model update), spot-check a few evening snap summaries in `evidence/<today>/` and extend the phrase list as needed. A more durable fix would add a structured `is_ir_mode` boolean to the `NestObservation` schema and have the analyzer prompt set it explicitly. Defer until production drift surfaces.

**b) Cost estimates remain rough.** Per-snap cost varies with prompt-cache hit rate (2026-04-15+ Anthropic SDK has caching), image size, and output token count. The constants are ballpark. If a heartbeat shows wildly different spend from the Anthropic console, recalibrate the constants from observed billing rather than re-deriving them from the prompt.

---

### 29. 2026-04-17 false-alarm hotfix: crest-hidden cardinal + egg-count unreliable

**Incident.** On 2026-04-17 production fired 35 alerts in a single day: 1 CRITICAL, 1 HIGH, 25 MEDIUM, 8 LOW. Seven of those were structurally false: 1 CRITICAL (egg_loss miscount on day 3-4 of incubation), 1 HIGH (crest-hidden cardinal classified as "unknown bird at nest"), and 5 MEDIUMs on frames where the cardinal was visibly in the cup but the analyzer couldn't confirm species. Root cause: the camera's low below/behind angle hides the cardinal's crest when she sits low, so Sonnet correctly returns `cardinal_on_nest="uncertain"` + `threat_species=["unknown"]`. The rules engine treated that ambiguous-bird-at-cup frame two ways at once — as absence (not confirmed true → MEDIUM `long_absence`) AND as threat (unknown + near_nest → HIGH `predator_near_nest`).

**Shipped fixes.** Three commits on `origin/main` (no analyzer prompt change — see §29d):

- `5b9be69` — Verifier content-aware suppression (Track 2). `verifier.py::is_cardinal_positive_no_threat(opus_obs)` returns True when Opus's `species_detected` contains "cardinal" AND `threat_species_detected` is empty. Short-circuits to `(None, opus_obs)` before the severity-rank comparison. This closes the 14:56 failure mode where Opus correctly identified the cardinal but had ALSO set `direct_nest_interaction=true` (schema violation on cardinal) → Opus's rule output CRITICAL-rank ≥ Sonnet HIGH → verifier confirmed the false HIGH.

- `f5b44be` — Rules+state hotfix (Tracks 1+4). Five changes:

  - **§29a ENABLE_EGG_COUNT_ALERTS flag** (`config.py`, `.env.example`, `events.py` rule 2). Default `false` on this deployment. Rule 2 egg_loss is silenced entirely — the camera cannot see into the cup reliably (eggs sit underneath the mother, or are occluded by the rim from the below/behind angle). Rule stays in code for a hypothetical future top-down camera; flip `ENABLE_EGG_COUNT_ALERTS=true` in `.env` to re-enable.

  - **§29b direct_nest_interaction invariant** (`events.py` rule 1). Rule 1 now requires `threat_species_detected` to be non-empty. A cardinal-positive observation with `direct_nest_interaction=true` (a schema violation by the analyzer — that field is defined for non-cardinal animals) can no longer produce a false CRITICAL on the cardinal's own tending behavior. Defense-in-depth alongside the §29 verifier suppression.

  - **§29c ambiguous-occupied-cup path** (`events.py::is_ambiguous_occupied_cup`, `state.py::record`, new `pending_ambiguous_frame_ts` column). Predicate: `nest_visible=true` AND `near_nest_activity=true` AND `cardinal_on_nest="uncertain"` AND `direct_nest_interaction=false` AND no NAMED threat species. When a frame matches, `evaluate()` returns `None` (no MEDIUM, no HIGH, no lifecycle transitions) and `state.py::record` stores the ts as a pending candidate. A second consecutive matching frame within 10 minutes (`_AMBIGUOUS_CONFIRM_WINDOW_S`) promotes to soft presence: clears `in_absence`, updates `last_mother_seen_ts`, clears pending. An unambiguous frame (clear cardinal / clear empty / named threat) clears the pending immediately. Load-bearing guardrails: (1) named threats bypass this path and fire threat rules directly; (2) `direct_nest_interaction=true` bypasses this path so unknown-species beak-in-cup still reaches CRITICAL; (3) the ambig early-return runs BEFORE `_lifecycle_event` so ambig frames can't leak into fledge/hatch detection.

  - **§29d chick confidence floor raised to 0.75** (`state.py::_CHICK_SIGHTING_CONFIDENCE_FLOOR`, both `state.py::record` and `events.py::_lifecycle_event` check this). Also: `mother_feeding_chicks=true` alone no longer counts as a chick signal for lifecycle advancement — only explicit `chicks_visible="true"` at ≥0.75 does. Prevents reddish-blob misreads at day 3-4 of incubation from creating a stale `first_chick_sighting_ts` that would bypass the 2-sighting guard on a real later hatch. (See §23 for the 2-sighting guard.) `mother_feeding_chicks=true` still records `last_feeding_event_ts` for the 30-min MEDIUM suppression during the feeding stage.

  - **§29e schema migration for `pending_ambiguous_frame_ts`** (`state.py::_SCHEMA_SQL` + `_migrations`). Idempotent ALTER TABLE path following the established pattern. New `tests/test_state_migration.py` covers the migration against a pre-column DB shape (not just a fresh scratch DB — Codex guardrail).

- `5c58bcb` — Chronological stateful replay (Track 5). `tests/integration/test_replay_2026_04_17.py` walks `evidence/2026-04-17/*` in ts order against a fresh scratch `StateStore`, calls `record()` + `record_alert()` per snap, and simulates the verifier using stored `verification.json` when present. 14 per-evidence-dir assertions verify specific false positives are now `None` and positive controls (mother_returned LOWs, genuine empty-nest MEDIUMs) still fire. Zero Anthropic API cost on replay — re-runnable.

**Don't regress** (these interact; all three must hold):

- The ambig-cup predicate MUST exclude `direct_nest_interaction=true` and any named threat species. Otherwise a single-frame real attack (unknown-species thrasher with beak in cup) gets silently suppressed. Codex P1 guardrail.
- The ambig-cup early-return in `evaluate()` MUST run BEFORE `_lifecycle_event()`. Otherwise crest-hidden frames can trigger fledge detection during feeding stage (cardinal_on_nest != "true" is a fledge precondition). Codex P2 guardrail.
- The verifier's content-aware override MUST check `threat_species_detected` is empty before suppressing. Otherwise a mixed "cardinal + thrasher" frame (thrasher chasing cardinal off the nest) gets silently suppressed.

**Impact verification.** The replay test compares pre-fix production alerts against post-fix decisions:

  Before: 35 alerts (1 CRITICAL, 1 HIGH, 25 MEDIUM, 8 LOW).
  After:  28 alerts (0 CRITICAL, 0 HIGH, 20 MEDIUM, 8 LOW).
  Net:    7 false alerts eliminated; zero real events suppressed.

**Deferred — Track 3 analyzer prompt rewrite.** An in-progress Bayesian prior ("bird sitting IN the cup + no thrasher features → cardinal by default") was held back from this hotfix because it over-corrected on `wm_mom_returning_02.jpg` in the lifecycle regression suite (got `cardinal_on_nest="true"` at 0.62 where expected was `"uncertain"`). Will re-ship once the prior is narrowed (require at least ONE partial cardinal feature or specific camera geometry) and the 13-image regression passes 13/13. Until then, a smaller class of false MEDIUMs remains — specifically frames where the analyzer's `near_nest_activity=false` disagrees with its own summary text ("bird in cup"). Example: 2026-04-17 13:15 and 13:20. The rules engine correctly trusts the structured field; only prompt tightening can fix this class.

**Operational clean-up that self-expired.** The 2026-04-17 15:23:26 false chick sighting recorded a stale `first_chick_sighting_ts` in production state. The existing 2-sighting guard's 4-hour window auto-invalidated it at 19:23:26 EDT — no manual DB cleanup was required. If a similar false sighting appears in the future AND a real hatch follows within 4 hours, the §29d raised confidence floor (0.75) makes that re-occurrence much less likely.

---

### 30. Secret rotation runbook + analytics-thread RO connection + supply-chain lockfile (2026-04-18)

Three related hardening items landed in the 2026-04-18 security pass.

**(a) Analytics-thread read-only SQLite connection.** `StateStore` now opens a **second** SQLite connection via `mode=ro` URI for the two analytics methods (`get_observations_in_window`, `get_alerts_in_window`). The writer connection (`self._conn`, autocommit, check_same_thread=False) is still used by every hot-path call on the asyncio event loop. The RO connection (`self._ro_conn`) runs on the analytics thread pool only. Without this split, the analytics thread could observe a partial state between the observations-row INSERT and the state-row UPDATE inside `record()` — autocommit means every statement commits immediately, but two statements in a row are not atomic across threads. The RO handle reads through WAL snapshots and never sees half-committed state. `close()` shuts down both connections. Do not route writes through `self._ro_conn` (it will fail with `attempt to write a readonly database` — that's the point). Do not remove the split "for simplicity" — the analytics thread pool exists precisely so a slow SQLite query can't block the alert hot path, and the RO split is what makes that safe.

**(b) Fixed-shape UPDATE in `tools/lifecycle_backfill.py`.** The backfill tool used to build an UPDATE string by joining a `list[str]` of fragments (`"egg_laying_started_ts = ?"` / `"incubation_started_ts = ?"`). Both fragments were hard-coded string literals, so no SQLi was reachable today, but the pattern is the wrong shape to leave in the tree — a future contributor letting user input drive column selection would re-open the door. Refactored to a single static SQL that always writes both columns, using the new value when the column was chosen to update and the existing value otherwise. Preserves the dry-run path and the "refuse to overwrite without --force" logic. The builder pattern is now banned in this module — any future column additions should extend the fixed SQL, not re-introduce dynamic fragments.

**(c) Supply-chain lockfile.** `requirements.lock` is now committed at repo root (`pip freeze --all` output). For CI / production reproducibility: `pip install -r requirements.lock` pins every transitive dep to exact versions. For normal dev work, `pip install -e .[dev]` (from `pyproject.toml`) is still the path. Regenerate the lockfile whenever you knowingly upgrade a dep: `source venv/bin/activate && pip freeze --all > requirements.lock && git add requirements.lock`. Don't let the lock drift silently from the venv — if `pip freeze` shows something not in the lock, either commit the new lock or figure out why an unexpected package landed in the venv.

**(d) Secret rotation runbook.** Three secrets live outside git; here's how to rotate each without an outage.

- **Anthropic API key.** Revoke the old key at https://console.anthropic.com/settings/keys (in the same workspace — keys are workspace-scoped, see §3). Create a new key in the same workspace. Edit `.env` and update `ANTHROPIC_API_KEY`. Restart the analyzer only: `launchctl kickstart -k gui/$(id -u)/com.cardinalnest.analyzer`. The downloader does not use Anthropic and keeps running. Verify new key works: `tail -F ~/Library/Logs/cardinal-nest-monitor/analyzer.out.log` and watch for the next snap getting a clean response from Sonnet. If the key is bad you'll see `AuthenticationError` in the log.

- **Discord webhooks.** For each affected channel (alerts / feed / analytics / backfill / lifecycle / test): in Discord, open channel settings → Integrations → Webhooks, delete the existing webhook, create a new one, copy the URL. Edit `.env` and update the corresponding `DISCORD_*_WEBHOOK_URL`. Restart analyzer only (downloader doesn't post to Discord): `launchctl kickstart -k gui/$(id -u)/com.cardinalnest.analyzer`. Smoke test: `source venv/bin/activate && python -m cardinal_nest_monitor.tools.test_discord` — sends a 🧪 embed to the alerts channel using the rotated URL.

- **Blink account password.** Change the password in the Blink mobile app. Locally, delete the cached credentials and re-run the 2FA flow:
  ```bash
  rm blink_credentials.json
  python -m cardinal_nest_monitor --auth-only
  # PIN is read from ~/.cache/cardinal_nest_monitor/blink_pin (see Agent 2 note)
  # or from the legacy /tmp/cardinal_nest_blink_pin fallback.
  ```
  Wait for the email → write the PIN to the file → script picks it up and writes a new `blink_credentials.json`. Restart downloader: `launchctl kickstart -k gui/$(id -u)/com.cardinalnest.downloader`. The analyzer does not auth to Blink so it's unaffected.

Don't skip the "restart only one service" part of each recipe — the whole point of the §20 decouple is that you can rotate an Anthropic key without interrupting snap capture, and rotate a Blink password without touching the Discord-facing analyzer.

---

## Cost levers (when daily heartbeat shows spend climbing)

Current realistic spend at the 2026-04-15 settings (single-tier Sonnet, 5/1/30 min dynamic cadence): **~$90/month**. Heartbeat reports cost-to-date.

Knobs in order of smallest impact first:

1. **Raise `ABSENCE_SNAP_INTERVAL_SECONDS`** from 60 (1 min) toward 120 or 180. Less aggressive during absence windows; still much better than the 5-min default. Trade-off: slower reaction during the actual risk window.
2. **Adjust `QUIET_HOURS` window** in `.env`. Currently `23:00-05:00` (6 hours quiet). Widen for more savings; narrow for more dawn-predator coverage.
3. **Raise `QUIET_SNAP_INTERVAL_SECONDS`** from 1800 (30 min) toward 2700 (45 min) or 3600 (60 min) for even less overnight coverage.
4. **Raise `SNAP_INTERVAL_SECONDS`** from 300 (5 min) toward 600 (10 min) or 900 (15 min). Linear cost reduction. Trade-off: bigger snake-detection blind window during the day when mom is present.
5. **Shorten `ACTIVE_HOURS`** — DON'T do this without explicit user buy-in; quiet hours is the better tool.
6. **Two-tier with Haiku re-enabled** — theoretically cheaper on high-volume boring snaps, but brings back the IR hallucination risk (§8). Only reconsider if snap volume spikes (e.g. motion detection re-enabled). See the two historical sections §8 and cadence table (PREFILTER_MODEL is still in config).
7. **Swap analyzer model** from `claude-sonnet-4-6` to `claude-haiku-4-5` — cheapest but unreliable on IR (see §8). Not recommended.

**Don't:** tighten the single-tier analyzer prompt in a way that makes it overconfident. Opus/Sonnet's "uncertain" verdict is load-bearing; it prevents false state updates (§5 known limitation).

---

## Known limitations (be honest with the user)

1. **Blink motion detection is currently OFF** (in the Blink app, not our code). mom's own movements on the nest were firing constant false triggers. `motion_loop` still runs but finds nothing. Motion-event reaction path is dormant; only scheduled snaps drive the pipeline.
2. **Scheduled-snap reaction floor** (now the only path):
   - Mom on nest: **up to 5 min** (SNAP_INTERVAL_SECONDS)
   - Mom absent (peak risk): **up to 1 min** (ABSENCE_SNAP_INTERVAL_SECONDS — Pattern A)
   - Quiet hours: **up to 30 min** (QUIET_SNAP_INTERVAL_SECONDS)
3. **Battery life: ~10–14 days** at current cadence with motion off. Camera only wakes on our snap_picture calls. Use `tools.pause` BEFORE walking near the nest.
4. **Vision model species accuracy is not expert-ornithologist.** Most likely confusions: female cardinal vs House Finch; Brown Thrasher vs Mockingbird. Evidence dirs enable post-hoc tuning of system prompts.
5. **Even Opus 4.6 makes false positives on IR night images.** Documented case 2026-04-13 22:52:19: Opus reported `cardinal_on_nest=true` at 60% confidence (just over the 0.55 threshold) on an image where the user verified there was no cardinal. The lighter shape on the right side of the nest cup was straw, not a bird. **Implication:** marginal-confidence "cardinal_on_nest=true" verdicts at night can pollute state and cause downstream blind spots in the predator-while-absent rule. The user chose to live with this for now (added model attribution to alert embeds for visibility), but **revisit raising the confidence threshold for state updates from 0.55 to 0.75** if false positives keep appearing. (See TODO at the end of this doc.)
6. **Daily heartbeat is the dead-man's switch.** No 🟢 startup embed after a launchd restart, no 📡 daily heartbeat at noon → system is down → check logs.
7. **Camera angle observation (as of 2026-04-13):** initial smoke-test snaps showed the nest at bottom-LEFT edge of frame. A later snap (22-27-54) showed the nest more centered, suggesting the angle was adjusted or the user repositioned. Either way: confirm via dryrun on a recent evidence/.../snap.jpg that the nest cup is well-framed before assuming alerts will be reliable.
8. **`launchctl kickstart -k` graceful shutdown takes ~80s.** SIGTERM is sent, but the in-flight snap+analyzer cycle has to finish before the asyncio event loop can exit. During this window, the OLD process keeps logging and posting. Wait for the "shutdown complete" line in out.log before considering a restart "done."
9. **No snake protection during quiet hours.** Quiet hours runs at 30-min cadence. A snake on the bush that doesn't fire PIR could sit at the nest for up to 30 min before being detected. The user accepted this trade-off; if a snake actually attacks, this is the most likely failure mode.

---

## Verifying before claiming "it works"

1. `TEST_MODE=true python -m pytest tests/ -v` → all 192 tests pass (154 unit + 38 integration). Includes the lifecycle 6-stage tests, IR-mode suppression tests, the codex round 1-5 regression guards, and the 2026-04-17 false-alarm hotfix coverage (§29): ENABLE_EGG_COUNT_ALERTS flag, direct_nest_interaction invariant, ambiguous-occupied-cup path (predicate + pending state + soft presence), chick confidence floor 0.75, schema migration test, verifier content-aware suppression, and the chronological stateful replay of the full 2026-04-17 production day.
2. `launchctl list | grep cardinalnest` → both `com.cardinalnest.downloader` and `com.cardinalnest.analyzer` show PIDs + exit code 0.
3. `tail ~/Library/Logs/cardinal-nest-monitor/downloader.out.log` → `Blink connected; N cameras`, `downloader watchdog started`.
4. `tail ~/Library/Logs/cardinal-nest-monitor/analyzer.out.log` → `spool consumer started`, `feed_worker started`, `analytics_scheduler started`, `watchdog started`.
5. Check primary Discord channel for 🟢 startup embeds from both services within ~10s.
6. Check feed Discord channel for first snap embed within one cadence cycle (~5 min day, ~30 min quiet).
7. `ls data/spool/pending/` → should be empty (analyzer drains faster than downloader writes).
8. **Sanity-check the analyzer on a recent snap:** `python -m cardinal_nest_monitor.tools.dryrun --image evidence/$(date +%Y-%m-%d)/<latest>/snap.jpg --escalate` — verify reasonable JSON output.
9. **Run the species-ID regression suite (before deploying ANY prompt change):** the four reference images in `evidence/reference/` — see §15 for the exact pass/fail criteria. Three must flag as threats, one (the cardinal FP) must NOT fire CRITICAL.
10. **Run `TEST_MODE=true python -m pytest tests/ -v` — mandatory before any non-trivial change to hot-path files** (`analyzer.py`, `events.py`, `main.py`, `blink_client.py`, `notifier.py`, `verifier.py`, `spool.py`, `downloader_loop.py`, `analyzer_loop.py`). See §18 for the full rule. No deploy without a green test suite.

---

## When to ask the user vs. just decide

- **Always ask** before destructive operations (deleting evidence, editing .env secrets, force-stopping launchd, re-auth that burns client_id slots).
- **Always ask** before changing model selection, snap cadence, active hours — these have cost and battery implications the user cares about.
- **Just do it** for: reading files, running tests, inspecting the SQLite state, tailing logs, trying a `tools.dryrun` against an existing JPEG.
- **Flag honestly** when something doesn't work as expected. The user values truth over reassurance — see how we handled the Anthropic credit confusion (didn't pretend, narrowed the cause to workspace mismatch, recommended creating a fresh key).

---

## TODOs deferred to a later session

These are real follow-ups the user explicitly deferred. Not nice-to-haves — actual improvements the user agreed are worth doing.

### TODO 1: MP4 frame analysis as a secondary verification path (deferred 2026-04-13)

**Problem:** motion-event reaction is ~60–120s because we wait for clip upload. The fresh `snap_picture()` we take after the clip is uploaded captures the CURRENT scene — but a fast in-and-out predator (lands, attacks, leaves in <30s) would be in the clip frames and gone by the time the snap fires. Currently we never analyze the clip frames; the MP4 just sits in `evidence/` for human review.

**Implementation sketch:**
1. After `download_clip()` lands the MP4, extract 2–3 frames via `cv2.VideoCapture` (e.g., frames at 5s, 15s, 25s into the clip).
2. Run them through `analyzer.analyze()` as a "post-hoc verification" pass.
3. If the post-hoc verdict materially disagrees with the snap-based decision (e.g., snap saw nothing but a clip frame shows a thrasher with `direct_nest_interaction=true`), post a follow-up Discord alert: `"⚠️ Post-hoc clip review: missed event detected"`.
4. Cost: small — only fires when motion event happened, and only adds 1–3 Opus calls per motion event. Probably +$10–30/month.

**Files to touch:** `blink_client.py` (the `download_clip` callback), maybe a new `clip_analyzer.py` module to keep concerns separate. `tests/` would need a fixture MP4 for testing.

**Caveat:** be careful about double-alerting. If the snap-based decision already correctly fired CRITICAL, the post-hoc verification shouldn't fire a second redundant alert. Probably gate on "post-hoc found something the snap missed" rather than "post-hoc agreed with snap."

### TODO 2: Confidence threshold for state updates (deferred 2026-04-13)

**Problem:** Opus made a false LOW alert at exactly 60% confidence (just over the 0.55 floor) on an ambiguous IR night image. The bigger issue isn't the LOW alert itself — it's that `cardinal_on_nest=true` at 60% confidence updated `last_mother_seen_ts` and reset `in_absence`, which creates a blind spot for the predator-while-absent HIGH rule.

**Implementation sketch:**
1. In `state.py` `record()`, raise the confidence threshold for `cardinal_on_nest=true` state updates from 0.55 to 0.75. (Other state updates stay at 0.55 — we want low-confidence threats acted on.)
2. Alternative: tier the confidence threshold by severity in `events.py` — CRITICAL/HIGH at 0.55, MEDIUM at 0.65, LOW at 0.75. Suppresses marginal LOW alerts entirely.
3. Either way: keep the current behavior of ALWAYS recording observations to disk regardless of confidence (we want the data for tuning later).

**Why deferred:** the user wanted visibility (model attribution in alerts) before tightening filters. Now they have visibility — collect a few days of data, then decide if the confidence floor should go up.

### TODO 3: Multi-snap consensus for state-changing events (lowest priority)

**Problem:** even with raised confidence, single-snap noise can flip state. A multi-snap consensus check (require 2 consecutive snaps to agree before flipping `in_absence` back to False) would reduce noise.

**Caveat:** at 5-min cadence this adds 5 min of latency to legitimate "mom returned" detection. Probably not worth it unless TODO 2 isn't enough. Decide after watching the data for a week.
