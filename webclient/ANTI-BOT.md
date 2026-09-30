# Web Anti-Bot: Detection & Evasion

_As of 2026-09-29. A synthesis of how modern web anti-bot systems detect automation, the
escalation tiers they impose, and the tiers we can deploy to avoid them — from bare HTTP requests
to a residential IP driving a real Chrome. Informs the design of the `web/fetch` browser layer._

> Provenance note: vendor **detection actions, score models, cookies and doc-level behaviour** are
> from primary docs. The **sensor internals** for Akamai, DataDome, HUMAN, Kasada and Imperva are
> largely from reputable third-party reverse-engineering (Scrapfly, Crawlex, ZenRows, etc.) — those
> vendors keep them proprietary. Such claims are marked _[RE]_ where it matters.

---

## 1. Executive summary

Modern web anti-bot is a contest between two escalation ladders, and both sides climb only as far
as they must. The **defender** starts by allowing traffic and escalates — passive fingerprint
scoring → an invisible JavaScript / proof-of-work challenge → an interactive challenge → a CAPTCHA →
a hard block — driven by a single risk **score** (Cloudflare 1–99 where low = bot; Akamai 0–100
where high = bot; DataDome and others via per-deployment ML) assembled from four signal families:
network/TLS fingerprints, IP and ASN reputation, browser fingerprints, and behaviour.

We climb the **opposite** ladder — from a bare HTTP request up to a residential or mobile IP driving
a _real_ Chrome — adding realness one rung at a time, and only as far as a given target forces us.
The economics are asymmetric: **detection is cheap, evasion is expensive.** Every rung up our ladder
costs more compute, bandwidth, or maintenance, and each one neutralises only a specific detection
layer.

The decisive practical rule is **coherence**: a request is judged on its weakest inconsistent
layer, not its best. A flawless browser fingerprint from an AWS IP is blocked on ASN before the
fingerprint is read; a residential IP with a single headless tell — a SwiftShader WebGL renderer,
the CDP `Runtime.enable` leak, an empty plugin array, or a Chrome-for-Testing binary — is blocked on
fingerprint. TLS, IP, the JavaScript surface, and behaviour must all tell the same story.

For `web/fetch` this means our transport tiers should form a **realness ladder that escalates on a
block**, and its top rung must be a genuine Chrome binary (not the bundled Chrome-for-Testing)
driven over CDP from a residential IP.

---

## 2. The detection surface

Providers score a request across four signal families, cheapest first — and a request is only as
trustworthy as its weakest, most inconsistent layer.

### 2.1 Network fingerprints (TLS + HTTP/2) — read before a byte of content

- **TLS ClientHello → JA3 / JA4.** JA3 is an MD5 of the TLS version, cipher list, extensions,
  curves and EC formats; JA4 is the SHA-256 successor that sorts extensions. A stock HTTP library
  (`httpx`/`requests` on OpenSSL, Go `net/http`) emits one fixed fingerprint that looks nothing like
  a browser's BoringSSL/NSS handshake — an instant blocklist hit, identical on every request.
  **Caveat:** since Jan 2023 Chrome randomises its TLS extension order per connection, so raw JA3 no
  longer pins modern Chrome — JA4 exists partly to survive that. A 2026 study classified bad bots
  from JA4 features alone at **98.6% accuracy (0.998 AUC)**.
- **HTTP/2 "Akamai fingerprint."** A second, independent layer: the SETTINGS frame keys and order,
  the WINDOW_UPDATE increment (Chrome `15663105` vs Firefox `12517377`), whether PRIORITY frames are
  sent, and pseudo-header order (Chrome `m,a,s,p` vs Firefox `m,p,a,s` vs Safari `m,s,p,a`). Catches
  a UA-spoofed client whose H2 stack doesn't match its claimed browser. Vendors combine TLS + H2, so
  spoofing one layer still mismatches the other.

### 2.2 IP / ASN reputation — the earliest, cheapest filter

Datacenter ASNs (AWS, GCP, Hetzner, OVH) are the single strongest predictor of automation and are
frequently blocked or hard-challenged on ASN _alone_, before any fingerprint is evaluated.
Residential and mobile (carrier-NAT) addresses inherit the reputation of the many real users who
share them — the reason they matter for hard targets.

### 2.3 Browser fingerprint — the JavaScript-observable surface

The entropy lives in active probes. In Eckersley's foundational 2010 study plugins carried 15.4
bits, fonts 13.9, the UA 10.0; canvas rendering adds ~10 bits, WebGL vendor/renderer ~6–8,
AudioContext ~4–5 — combined well over 18 bits. But real-world _uniqueness_ is far lower at
population scale: **83.6% of browsers were unique** in that self-selected desktop sample versus only
**33.6%** in a 2-million-fingerprint general-population sample, because mobile devices collapse into
a handful of common configurations. Any entropy budget must cite population scale.

The automation **tells** that unmask a headless browser are separate from entropy:

- `navigator.webdriver` (set under automation flags);
- the CDP **`Runtime.enable` serialization leak** — the flagship vector; it reveals DevTools control
  with no other check, used by Cloudflare/DataDome independent of any other signal;
- a missing/malformed `window.chrome`;
- an empty `navigator.plugins` / `mimeTypes`;
- `userAgentData` / Client-Hints inconsistency (UA vs `Sec-CH-UA` vs `navigator.platform`);
- and hardest to fake — a **SwiftShader** WebGL renderer (software rendering on a GPU-less server),
  plus the **Chrome-for-Testing** binary's missing Widevine DRM and proprietary codecs, which are
  compiled-in capabilities no JavaScript patch can forge.

Coherence beats any single value: **FP-Scanner** recovers the _real_ OS/browser from the leftover
inconsistencies of piecemeal spoofing (UA override + canvas noise + platform override that don't
jointly add up), and canvas-noise injection is itself a detectable statistical signature.

### 2.4 Behaviour and challenges — the session layer

The hardest layer to fake cheaply:

- **Mouse dynamics** — a neuromotor (Sigma-Lognormal) model detects even GAN-synthesised
  trajectories at ~93%.
- **Keystroke timing** — bots cluster at fixed intervals (e.g. 0.05s/0.25s); humans vary.
- **Session navigation graphs** — a graph-CNN over site traversal is more evasion-resistant than any
  single-request signal (~98%), because it models the whole session, not one request.
- **Active challenge** — an invisible JS / proof-of-work probe that escalates to a CAPTCHA only when
  the passive score is ambiguous.

Two caveats shape everything below. Fingerprint _instability_ is not a defence — **FP-Stalker**
re-links browsers across days of drift (50% change within 5 days, yet tracked ~54 days on average).
And fingerprinting is now deployed on roughly **10% of the top 100K sites and 30% of the top 1K**
(FP-Inspector).

---

## 3. The major providers

Six enterprise stacks dominate, and they converge on one architecture — a client-side JS **sensor**
collecting fingerprints + behaviour, a named **cookie/token** carrying session trust, and a risk
**score** driving escalating actions. They differ mainly in how resistant the sensor is to reverse
engineering, which is the axis their reputations turn on.

| Provider | Score model & client artifact | Response tiers | Bypass difficulty (why) |
| --- | --- | --- | --- |
| [Cloudflare Bot Management](https://developers.cloudflare.com/bots/) | Bot score **1–99** (low = bot); `__cf_bm`, `cf_clearance` | Allow · Log · Managed Challenge · Block; Turnstile | Moderate–Hard — heuristics + ML + JS Detections + JA3/JA4; Managed Challenge is adaptive |
| [Akamai Bot Manager](https://techdocs.akamai.com/cloud-security/docs/detection-methods) | Score **0–100** (high = bot); `_abck`, `bm_sz` (sensor `bmak`) | Monitor · Alert · Tarpit · Deny · Serve-alternate · CAPTCHA | Hard — `_abck` flips untrusted→validated on the sensor POST; Premier adds behavioural |
| [DataDome](https://docs.datadome.co/docs/response-pages) | Per-customer **ML**; `datadome` (`tags.js`) | Device Check · CAPTCHA · Block | Very Hard — ~190 signals, framework-specific bot fingerprints, frequent script rotation |
| [HUMAN / PerimeterX](https://docs.humansecurity.com/applications/use-of-cookies-web-storage) | Server-side Risk API; `_px` / `_pxvid` / `_pxhd` (sensor `bello`) | Monitor · Active block · Press-and-Hold | Hard — 100–200+ behavioural signals; captcha often inside a closed shadow root |
| [Kasada](https://www.kasada.io/javascript-deobsfusction-bot-defenses/) | Escalating **PoW**; `x-kpsdk-ct` / `-cd` (`ips.js` VM) | PoW slowdown · Block | **Hardest** — sensor runs in a custom bytecode VM; 128-bit PoW; anti-debugging; time-locked |
| [Imperva / Incapsula](https://www.imperva.com/resources/datasheets/Datasheet-Advanced-Bot-Protection.pdf) | JS challenge; `reese84`, `incap_ses`, `visid_incap` | Allow · Monitor · Block | Moderate–Hard — strong IP/ASN reputation, but `reese84` is conventionally-obfuscated JS, not a VM |

**Relative bypass difficulty (cross-source consensus):** Kasada (hardest — bytecode-VM sensor, not
deobfuscatable JS) > DataDome (per-customer ML) > HUMAN/PerimeterX (widest behavioural telemetry) >
Imperva (easiest — strong IP reputation but a conventionally-obfuscated collector).

The **CAPTCHA layer** is not a separate provider tier but the component the stacks above escalate
_into_: [reCAPTCHA](https://developers.google.com/recaptcha/docs/v3) v2/v3/Enterprise,
[hCaptcha](https://docs.hcaptcha.com/), [Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/),
and [Arkose MatchKey](https://www.arkoselabs.com/arkose-matchkey/). All are score-first and invisible
unless suspicious; only Arkose routinely shows an always-on interactive puzzle (adversarially-perturbed
images plus a session-bound proof-of-execution), reserved for account-security flows. The economics
now favour the attacker at the _puzzle_ alone — human-farm token services return a valid token in
10–30s and ML solvers clear reCAPTCHA v2 image grids at very high rates — which is exactly why every
vendor demoted the visible puzzle to a fallback behind the score and binds each token to
sitekey + action + hostname + a short single-use expiry to stop replay.

---

## 4. The provider escalation ladder (what the defender imposes)

Low → high. The design goal every vendor shares is **"invisible unless suspicious"**: keep the
common path frictionless and reserve the expensive tiers for traffic that fails the passive checks.
Escalation is mostly per-request; session trust is carried in a cookie (`cf_clearance` / `_abck` /
`datadome`).

```
  ▲ HARD BLOCK        403 Deny · Tarpit (delay) · Serve-alternate (fake 200)
  │                   trigger: high-confidence bot score / repeated challenge failure
  │ CAPTCHA           image grid · slider · Arkose press-and-hold
  │                   trigger: actively suspicious score, or always-on for high-value flows
  │ INTERACTIVE       one-click checkbox / Managed Challenge
  │ CHALLENGE         trigger: score in the grey zone
  │ INVISIBLE JS /    silent script must execute + return a token (Turnstile, DataDome Device
  │ PROOF-OF-WORK     Check, Kasada ips.js PoW, Akamai sensor); unmasks headless, imposes CPU cost
  │                   trigger: ambiguous score / no valid clearance cookie
  │ PASSIVE SCORING   TLS(JA4) + HTTP2 + IP/ASN reputation + (once JS runs) the browser fingerprint,
  │                   combined into a risk score — no user friction; trigger: every request
  ● ALLOW             traffic below the suspicion threshold passes untouched
```

Two provider-specific notes: Akamai frames this as an explicit three-tier response (Cautious →
Monitor, Strict → Challenge, Aggressive → Deny), and treats Tarpit/Serve-alternate as deliberately
stealthy "hard" tiers that don't tip off the attacker. Cloudflare is less a continuous ladder than a
per-request rule evaluation against a score, with `__cf_bm` smoothing the score across a session.

---

## 5. Our evasion ladder (what we can deploy)

Cheapest → strongest. Each rung adds realness and cost, and defeats one more detection layer.

| # | Rung | Defeats | Still caught by | Rough cost |
| --- | --- | --- | --- | --- |
| 1 | **Bare HTTP** (`httpx`/Go, spoofed UA only) | Crude UA-string checks | JA3/JA4 + HTTP2 fingerprint; zero JS; datacenter IP | ~free, 1000s req/s |
| 2 | **+ TLS/HTTP2 impersonation** (curl_cffi, curl-impersonate, utls/tls-client) | JA3/JA4 + HTTP2 SETTINGS/priority/pseudo-header | No JS execution → any JS challenge fails; stale impersonation profile; IP | ~free |
| 3 | **Headless Chromium** (Playwright/Puppeteer default) | JS-challenge execution, basic header checks | `navigator.webdriver`, `Runtime.enable` leak, empty plugins, SwiftShader WebGL, CfT codec/Widevine gaps, automation flags | compute-heavy |
| 4 | **+ Stealth patches** (puppeteer-extra-stealth, playwright-stealth) | Common JS-property tells (webdriver, chrome, plugins, permissions) | `Runtime.enable` leak (protocol-level); the stealth plugin is itself fingerprintable; SwiftShader/CfT | compute-heavy |
| 5 | **+ CDP-leak-safe driving + coherent fingerprint** (nodriver / patchright / rebrowser-patches + browserforge) | The `Runtime.enable` side-channel; cross-field fingerprint inconsistency; stripped automation flags | Bundled/CfT binary tells (Widevine/codecs); software GPU; datacenter IP | compute + maintenance |
| 6 | **+ Real Chrome binary** (not CfT; attached via CDP or headed under Xvfb, real GPU) | Widevine/codec/branding probes; SwiftShader WebGL tell; headless rendering gaps | **IP reputation is independent** — a perfect browser from a flagged ASN is still challenged | licensing/ops heavy |
| 7 | **+ Residential/mobile IP + human-like behaviour** (mouse/scroll/dwell, realistic nav graph) | ASN/IP filtering; behavioural/ML risk scoring | Session-level ML drift; CAPTCHA/PoW on the highest-risk flows; identity-graph signals | $15–30+/GB mobile |
| 8 | **+ CAPTCHA solver** (token service / ML, for score-gated challenges) | Score-gated CAPTCHAs (bound token) | Always-on Arkose puzzles stay costly/fragile — avoid the flow rather than solve it | per-solve |

**Cost/complexity gradient.** Rungs 1–2 are near-free and scale to thousands of req/s but only clear
naive/no-JS targets. Rungs 3–6 require real browser processes (compute-heavy, low throughput) and
clear mid-tier vendors, but enterprise vendors checking Widevine/codec/GPU coherence still catch
them. Rung 7 is where **cost-per-successful-request** dominates: expensive residential/mobile
bandwidth + behavioural emulation is the only tier that reliably clears top-tier enterprise anti-bot
(Akamai, HUMAN, Kasada, Cloudflare Enterprise) — and even then it's a continuous maintenance race.

### Key sub-findings for our build

- **Chrome for Testing ≠ real Chrome.** Playwright/Puppeteer bundle Chromium or Chrome-for-Testing,
  which lack Widevine (CDM) and — on unbranded builds — H.264/AAC. `MediaSource.isTypeSupported()`
  and EME probes are compiled-in capabilities no JS stealth can fake.
- **The `Runtime.enable` leak** is the single most exploited vector. Puppeteer/Playwright call it on
  every frame; a page can plant a getter that fires only when Runtime instrumentation is active.
  Fixes: `enable`→`disable` immediately, `addBinding`, or isolated worlds — which is what
  `nodriver`/`patchright`/`rebrowser-patches` do structurally.
- **Pointing `executablePath` at real Chrome is not enough** — Playwright still injects automation
  flags (`--enable-automation`, `--disable-extensions`, …). The launch config must be stripped down.
- **Coherent fingerprint injection** (browserforge / Apify fingerprint-suite) matters because
  cross-field _inconsistency_ is the tell — a Bayesian model keeps correlated fields correlated.
- **Headed Chrome under Xvfb** (a virtual display) is reported as the only Chromium config reaching
  single-digit detection scores on public scanners, mainly by getting a real GPU/WebGL string.

---

## 6. Where each of our tiers stops working

Each rung dies at a specific layer: bare HTTP at the first heuristic, TLS impersonation at any JS
challenge, stealthy headless at the enterprise sensor (behaviour + Widevine/WebGL), a real Chrome at
the datacenter IP, and only a real Chrome on a residential IP with human-like behaviour clears the
enterprise tier — with an always-on CAPTCHA still needing a solver. `✓` passes, `~` target-dependent,
`✗` blocked.

| Our tier ↓ &nbsp; vs posture → | No bot mgmt | UA + IP heuristics | JS + TLS/H2 challenge (std CF/DataDome) | Behavioural + hard sensor (Akamai/PX/Kasada) | Always-on CAPTCHA / Arkose |
| --- | :---: | :---: | :---: | :---: | :---: |
| 1 Bare HTTP | ✓ | ✗ | ✗ | ✗ | ✗ |
| 2 + TLS/HTTP2 impersonation | ✓ | ~ | ✗ | ✗ | ✗ |
| 3 Headless Chromium (no stealth) | ✓ | ~ | ~ | ✗ | ✗ |
| 4 + Stealth patches | ✓ | ✓ | ~ | ✗ | ✗ |
| 5 + CDP-leak-safe + coherent fingerprint | ✓ | ✓ | ✓ | ~ | ✗ |
| 6 + Real Chrome (real GPU, not CfT) | ✓ | ✓ | ✓ | ~ | ✗ |
| 7 + Residential/mobile IP + human behaviour | ✓ | ✓ | ✓ | ✓ | ~ |
| 8 + CAPTCHA solver | ✓ | ✓ | ✓ | ✓ | ~ |

The two jumps that matter most for us: **tier 4→5** (closing the CDP `Runtime.enable` leak and making
the fingerprint coherent) clears a _standard_ Cloudflare or DataDome deployment; **tier 6→7** (a real
Chrome plus a residential/mobile IP and real interaction timing) is the only step that clears the
_enterprise_ behavioural tier — and no fingerprint work substitutes for the IP, because ASN is checked
first. The last column never fully closes: a score-gated CAPTCHA can be passed with a bound token, but
Arkose-class always-on puzzles stay costly and fragile, so the right answer there is usually to avoid
the flow, not solve it.

---

## 7. Implications for `web/fetch`

Our transport layer should treat realness as an escalation ladder that mirrors the provider's own and
stops climbing the moment a tier passes. Four requirements this research makes non-negotiable:

1. **A real Chrome binary, not Chrome-for-Testing.** The bundled CfT/Chromium is a compiled-in tell
   (missing Widevine, codec gaps, branding) that no JavaScript patch can hide. The top rung must
   launch or attach to genuine installed Chrome — which is why the abstraction has to install and
   manage real Chrome binaries, not only slim drivers.
2. **Drive over CDP without the `Runtime.enable` leak.** Attaching to a real Chrome over CDP removes
   the bundled-binary and launch-flag tells, but only if the driver never issues the leaky
   `Runtime.enable` sequence — so we own the CDP client (nodriver / patchright-style), not a stock
   Playwright launch, and we support attaching to a _remote_ CDP endpoint (a real Chrome on another
   host or a residential-exit box).
3. **Coherent fingerprint + stealth, holistically.** Piecemeal spoofing is detectable (FP-Scanner).
   We already inject browserforge fingerprints and patch `userAgentData` / `plugins` / `window.chrome`
   and strip automation flags; the remaining gaps are a real GPU (no SwiftShader — i.e. headed under a
   real or virtual display) and version-currency (track Chrome releases; a stale "current" UA is
   itself a signal).
4. **IP is a first-class tier, above the browser.** Because ASN is checked before fingerprint, the
   top rungs must pair the real browser with a residential/mobile egress; a perfect browser on a
   datacenter IP is the most common wasted effort.

**The abstraction to build (next task):** a Browser layer that (a) manages the Chrome process / CDP
connection lifecycle, (b) attaches to a remote CDP process, (c) installs the required binaries — slim
chromedrivers _and_ real Chrome — and (d) exposes a clean interface `web/fetch` plugs into, so the
existing `FetchProfile` realness ladder (`BROWSER` → `HEADED_BROWSER` → `REAL_CHROME`) resolves to
genuinely distinct, genuinely-real backends instead of one bundled Chromium wearing different flags.
For most onboarding targets a coherent, stealthy headless Chrome (tier 5) already clears standard
Cloudflare/DataDome, so it stays the default; the real-Chrome and residential-IP rungs are on-demand
escalation. The rule stands: **escalate _realness_, never fall back to plain HTTP** — a blocked
browser is not yet real enough, not proof that HTTP is safer.

Status already shipped (commit `e65d3a9`): the browser now uses full browserforge injection +
supplemental patches (`userAgentData.brands`, `navigator.plugins`, `window.chrome`) + stealth launch
args; measured tells are clean (webdriver false, real Chrome UA, populated brands, 5 plugins, real
GPU via ANGLE); the WAF-protected IR host that 403-d headless now returns 200; and the realness
ladder exists as fetch profiles with best-effort tier-open.

---

## 8. Sources

### Academic papers (highest priority)

- [How Unique Is Your Web Browser? — Eckersley, PETS 2010](https://www.freehaven.net/anonbib/papers/pets2010/p1-eckersley.pdf)
- [The Web Never Forgets — Acar et al., CCS 2014](https://mjuarezm.github.io/assets/pdf/never-ccs14.pdf)
- [FPDetective — Acar et al., CCS 2013](https://lirias.kuleuven.be/retrieve/d6e8af48-568d-4c33-8155-8fe24147bf9f/)
- [Hiding in the Crowd — Gómez-Boix, Laperdrix, Baudry, WWW 2018](https://dl.acm.org/doi/10.1145/3178876.3186097)
- [FP-Stalker: Tracking Browser Fingerprint Evolutions — Vastel et al., IEEE S&P 2018](https://www.usenix.org/system/files/conference/usenixsecurity18/sec18-vastel.pdf)
- [FP-Scanner: Browser Fingerprint Inconsistencies — Vastel et al., USENIX Security 2018](https://www.usenix.org/system/files/conference/usenixsecurity18/sec18-vastel.pdf)
- [Fingerprinting the Fingerprinters — Iqbal, Englehardt, Shafiq, IEEE S&P 2021](https://arxiv.org/pdf/2008.04480)
- [Browser Fingerprinting: A Survey — Laperdrix et al., ACM TWEB 2020](https://www-sop.inria.fr/members/Nataliia.Bielova/papers/Lape-etal-20-TWEB.pdf)
- [Fingerprint Surface-Based Detection of Web Bot Detectors — Jonker, Krumnow, Vlot, ESORICS 2019](https://cs.ou.nl/members/hugo/papers/ESORICS19.pdf)
- [Passive Fingerprinting of HTTP/2 Clients — Shuster, Fridman, Segal (Akamai), Black Hat EU 2017](https://blackhat.com/docs/eu-17/materials/eu-17-Shuster-Passive-Fingerprinting-Of-HTTP2-Clients-wp.pdf)
- [When Handshakes Tell the Truth: Detecting Web Bad Bots via TLS — 2026 preprint](https://arxiv.org/pdf/2602.09606)
- [BOTracle: Discriminating Bots and Humans — 2024/2026 preprint](https://arxiv.org/pdf/2412.02266)
- [BeCAPTCHA-Mouse: Synthetic Mouse Trajectories — Acien et al., 2020](https://arxiv.org/pdf/2005.00890)
- [Breaking reCAPTCHAv2 — arXiv 2409.08831](https://arxiv.org/pdf/2409.08831)
- [I Am Robot: Learning to Break Semantic Image CAPTCHAs — Sivakorn et al., EuroS&P 2016](http://www.cs.columbia.edu/~polakis/papers/sivakorn_eurosp16.pdf)
- [CAPTCHAs in the Agentic Era — arXiv 2609.02393](https://arxiv.org/html/2609.02393v1)
- [Broken Gates: Web Bot Defenses in the Age of LLM Agents — arXiv 2607.18659](https://arxiv.org/pdf/2607.18659)

### Provider documentation

- Cloudflare: [Bot scores](https://developers.cloudflare.com/bots/concepts/bot-score/) ·
  [Detection engines](https://developers.cloudflare.com/bots/concepts/bot-detection-engines/) ·
  [JA3/JA4](https://developers.cloudflare.com/bots/additional-configurations/ja3-ja4-fingerprint/) ·
  [Turnstile](https://developers.cloudflare.com/turnstile/) ·
  [Clearance](https://developers.cloudflare.com/cloudflare-challenges/concepts/clearance/)
- Akamai: [Detection methods](https://techdocs.akamai.com/cloud-security/docs/detection-methods) ·
  [Handle adversarial bots](https://techdocs.akamai.com/cloud-security/docs/handle-adversarial-bots) ·
  [Define bot responses](https://techdocs.akamai.com/terraform/docs/set-up-botman)
- DataDome: [JavaScript Tag](https://docs.datadome.co/docs/javascript-tag) ·
  [Device Check](https://docs.datadome.co/docs/device-check) ·
  [Response pages](https://docs.datadome.co/docs/response-pages)
- HUMAN/PerimeterX: [Cookies & Web Storage](https://docs.humansecurity.com/applications/use-of-cookies-web-storage) ·
  [Fastly VCL config](https://docs.humansecurity.com/applications/fastly-vcl/v12/configuration)
- Kasada: [JS Deobfuscation / bot defenses](https://www.kasada.io/javascript-deobsfusction-bot-defenses/)
- Imperva: [Advanced Bot Protection datasheet](https://www.imperva.com/resources/datasheets/Datasheet-Advanced-Bot-Protection.pdf) ·
  [Bad Bot sophistication levels](https://www.imperva.com/blog/infographic-bad-bot-sophistication-levels/)
- CAPTCHA: [reCAPTCHA v3](https://developers.google.com/recaptcha/docs/v3) ·
  [reCAPTCHA Enterprise assessments](https://docs.cloud.google.com/recaptcha/docs/interpret-assessment) ·
  [hCaptcha](https://docs.hcaptcha.com/) ·
  [Turnstile widget modes](https://developers.cloudflare.com/turnstile/concepts/widget/) ·
  [Arkose MatchKey](https://www.arkoselabs.com/arkose-matchkey/)

### Evasion tooling & technical analysis

- Fingerprinting/detection: [JA3/JA4 guide (Scrapfly)](https://scrapfly.io/blog/posts/ja3-ja4-tls-fingerprinting-guide-to-detection-and-evasion) ·
  [HTTP/2 & HTTP/3 fingerprinting (Scrapfly)](https://scrapfly.io/blog/posts/http2-http3-fingerprinting-guide) ·
  [Chrome CDP stealth (Scrapfly)](https://scrapfly.io/blog/posts/chrome-cdp-stealth-browser-automation-detection-explained) ·
  [WebGL renderer in fingerprinting (Castle)](https://blog.castle.io/the-role-of-webgl-renderer-in-browser-fingerprinting/) ·
  [bot.incolumitas.com detection tests](https://bot.incolumitas.com/)
- Tools: [curl_cffi](https://github.com/lexiforest/curl_cffi) ·
  [tls-client (bogdanfinn)](https://github.com/bogdanfinn/tls-client) ·
  [rebrowser-patches](https://github.com/rebrowser/rebrowser-patches) ·
  [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) ·
  [puppeteer-extra-plugin-stealth](https://www.npmjs.com/package/puppeteer-extra-plugin-stealth) ·
  [fingerprint-suite (Apify)](https://github.com/apify/fingerprint-suite) ·
  [browserforge](https://pypi.org/project/browserforge/) ·
  [Kameleo](https://github.com/kameleo-io/kameleo)
- "Chromium is not Chrome": [invisible_playwright wiki](https://github.com/feder-cr/invisible_playwright/wiki/chromium-is-not-chrome) ·
  [Headless Chrome detection (Crawlex)](https://blog.crawlex.net/blog/headless-chrome-detection/)
- Provider reverse-engineering _[RE]_: [Akamai _abck / sensor (Scrapfly)](https://scrapfly.io/blog/posts/akamai-bot-manager-understanding-abck-cookies-and-sensor-data) ·
  [DataDome bypass (Browserless)](https://www.browserless.io/blog/datadome-bypass) ·
  [PerimeterX VID/sensor (Crawlex)](https://blog.crawlex.net/blog/perimeterx-vid-sensor-bello/) ·
  [Kasada KPSDK/VM (Crawlex)](https://blog.crawlex.net/blog/kasada-kpsdk-token-vm/) ·
  [Imperva reese84 (Crawlex)](https://blog.crawlex.net/blog/imperva-reese84-sensor/) ·
  [How to bypass anti-bot in 2026 (Scrapfly)](https://scrapfly.io/blog/posts/how-to-bypass-anti-bot-protection)
