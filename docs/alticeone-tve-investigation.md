# Optimum TV / AlticeOne TVE investigation

Base: FastChannels **v5.2.2**, commit `5dc647e02a740b071af5e3bf0e65d39276e32518`.
Branch: `fix/alticeone-discovery-history-amc` in `cstukane/FastChannels`.
Investigation date: 2026-09-15. Provider identity remains **AlticeOne**.

## Discovery: reproduced before the fix

An anonymous bootstrap using the actual v5.2.2 scraper returned:

1. Discovery `/token`, `/v1/gauth/partners`, `/v1/gauth/authorize`: HTTP 200.
2. Adobe `api.auth.adobe.com/api/v1/authenticate`: HTTP 302.
3. Adobe `sp.auth.adobe.com/api/v1/authenticate`: HTTP 200 with a POST form
   targeting `idpssoalt.alticeusa.com`. Field names include `SAMLRequest` and
   `RelayState`; values were neither logged nor saved.
4. The scraper raised `TVEAuthError` because no `Location` header existed.

The live URL explicitly selected `mso_id=AlticeOne`, `requestor_id=discovery`.
Discovery supplies its authenticate URL via gauth; FastChannels should not
replace that requestor or invent an Adobe resource for this flow.

The browser-assisted path now hands the original gauth target to the browser,
as NBC already does for Adobe authenticate URLs. The browser owns redirects,
SAML form submission and Adobe/MVPD cookies. The scripted Cox/Xfinity/DIRECTV
paths retain their existing behavior. Exact AlticeOne partner matching is
required so name fallback cannot select legacy Optimum/Cablevision.

## History / A+E: mapping verified, account entitlement unresolved

Live public pages and their JavaScript were inspected before changing code:

| Official page | Requestor | Resource title | Statement selection |
| --- | --- | --- | --- |
| `https://play.history.com/live` | HISTORY | HISTORY | `adobeSoftwareStatement.history` |
| `https://play.mylifetime.com/live` | LIFETIME | LIFETIME | `adobeSoftwareStatement.lifetime` |
| `https://play.aetv.com/live` | AETV | AETV | `adobeSoftwareStatement.aetv` |

The public player builds an MRSS channel resource with this title and optional
item title/GUID/rating from the playing video. FastChannels uses the same channel
title with an empty item for its channel-level authorization. No incorrect
History/Lifetime requestor or swapped statement was found. FYI's live URL
redirected to a page without Next.js config; sibling pages still carried the
separate `fyi` statement. FYI's live playback mapping was not independently verified.

The reported History message comes from an Adobe `notAuthorized` response after
authentication, not from failure to reach Optimum. The code continues to honor
that denial. Diagnostics now identify authorization stage, known requestor,
resource-title agreement, whether the MVPD is AlticeOne, HTTP status and denial;
they do not print the authn token, resource XML or provider response body.

**This account's entitlement cannot be determined without authenticated live
testing.** The matching channel identifiers make a subscription/provider TVE
entitlement issue plausible, but the empty channel-level MRSS item versus the
official player's optional program metadata remains a comparison point if the
same account can play History on its official site. A History denial alone does
not prove a subscription issue. If the correct resource is denied there too,
the issue belongs with Optimum/A+E; do not bypass or substitute Lifetime's grant.

### Software-statement discovery

The old parser worked against today's History/Lifetime/A&E responses, so the
reported discovery failure was not reproduced live. Code inspection confirmed
brittleness: an exact script opening tag, one config nesting, limited JS syntax,
and only one sibling fallback. It now parses script elements independent of
attribute order/nonce/quotes, finds brand-keyed structured config, supports
quoted/spaced inline and bundle maps, checks bundle HTTP status, and tries the
other official A+E pages. Brand matching is bounded to the statement map; it
cannot scan across another object's braces. Discovery still uses live statements,
the existing cache/invalidation, and Adobe's registration validation. No new
statement or credential is hardcoded.

## AMC: reproduced before the fix

The actual v5.2.2 AMC registration produced a code and authenticate URL without
any prior MVPD cache. Fetching that URL returned HTTP 200 with an Optimum TV
SAML POST form targeting `idpssoalt.alticeusa.com`. The scraper rejected it
because only DIRECTV was exempt from the `Location` requirement.

The browser launches before registration; four rejected registrations can make
it appear to open and close without ever reaching a login. The fix hands each
new requestor-specific authenticate URL to the persistent browser directly.
The v5.2.2 browser entry point does **not** require an existing auth cache, and
the earlier quoted “no cached sign-in” error is not present in this tag's AMC
browser path. It is not the reproduced v5.2.2 root cause.

Completion remains server-authoritative: poll the selected MVPD's Adobe profile,
then request the channel's authorization decision. A grant for a different
resource cannot complete pairing; a malformed/missing decision or missing token
is an authentication/protocol error, not a subscription denial. Only an explicit
deny for the requested resource is classified as not entitled. Cached-session
denials are propagated without restarting sign-in.

Each successful requestor saves its own `adobe_session:<requestor>` (code/bearer)
and `adobe_auth:<requestor>` (media token/user identity) for the selected MVPD.
The existing `/data/browser_profiles/mvpd_tve` profile persists browser cookies
and can let Adobe/Optimum perform legitimate SSO. HISTORY/LIFETIME authn/authz
tokens are not copied into AMC/BBCA/IFC/WETV caches. Cookie SSO is an opportunity
to authenticate; it is not an entitlement grant. Browser URL/error diagnostics
in these paths omit query strings and raw browser exception bodies.

Adobe's [authorization decision API documentation](https://experienceleague.adobe.com/en/docs/pass/authentication/integration-guide-programmers/rest-apis/rest-api-v2/rest-api-v2-apis/rest-api-v2-decisions-apis/rest-api-v2-decisions-apis-retrieve-authorization-decisions-using-specific-mvpd)
specifies the per-decision `resource` and `authorized` fields.

## Separate Docker test container

The upstream Dockerfile and production publishing workflow are unchanged. The
new compose file builds a local test tag and uses a separate Compose project,
port and named volumes. Run from a checkout of this branch with Docker running:

```sh
git fetch origin fix/alticeone-discovery-history-amc
git switch fix/alticeone-discovery-history-amc
docker compose -f docker-compose.alticeone.yml build
docker compose -f docker-compose.alticeone.yml up -d
```

Open `http://localhost:5524/admin/`. For a remote Docker host, use an SSH tunnel
to its loopback port 5524 or deliberately adjust the test port binding.
Configure Optimum TV (`AlticeOne`) using the app's Settings UI. These are fresh
test volumes; enter account credentials there rather than copying production
data or adding credentials to Compose. To stop while retaining test sessions:

```sh
docker compose -f docker-compose.alticeone.yml down
```

The local image is `fastchannels:alticeone-v5.2.2-test`. No registry publishing
is needed. An alternative image-only build is:

```sh
docker build --tag fastchannels:alticeone-v5.2.2-test .
```

## Validation and remaining live checks

The v5.2.2 tag contains no tracked Python test suite; initial
`python -m unittest discover -v` found zero tests. New offline tests use synthetic
HTTP responses and mocked browser/Redis/database boundaries. They test actual
scraper and browser orchestration code; they are not real Optimum sign-ins.

Run the regression suite with `python -m unittest discover -s tests -v`.

Validation results:

- Regression suite: **27 tests passed** on Python 3.13.5 (Windows).
- `python -m compileall -q` for changed Python modules and tests: passed.
- `docker compose -f docker-compose.alticeone.yml config --quiet`: passed.
- `git diff --check`: passed.
- Patched anonymous live Discovery handoff: passed, still `AlticeOne`.
- Patched anonymous live AMC handoff: passed, real registration code and Adobe
  authenticate URL returned (values not logged).
- Live statement discovery for History, Lifetime, A&E and FYI: passed.
- Image build, container restart, real browser SAML completion and authenticated
  authorization/playback: **not run**, for the environment reasons below.

Required isolated live acceptance:

1. Start with fresh test data. Discovery must reach the Optimum TV login, finish
   the SAML callback/code exchange, and save its session only after success.
2. Start AMC without any prior TVE sign-in. For AMC/BBCA/IFC/WETV separately,
   confirm pairing or an explicit denial. A page close alone is not success.
3. Restart the test container; verify persisted sessions and short-token renewal.
   Expired/revoked sessions should require a fresh real login.
4. Compare History versus Lifetime and official History playback on the same
   account. If only FastChannels fails, compare the exact requested MRSS resource
   privately, including optional program metadata. Do not publish a raw HAR.
5. Recheck the known working Lifetime, TNT, TBS, truTV, NBC and FOX controls.
   Offline Cox tests verify the original scripted handoff, not live provider
   availability or full playback.

No account credentials were available during this investigation. Docker engine
was unavailable on this host, so a real image build/container run and authenticated
AlticeOne acceptance remain unrun.
