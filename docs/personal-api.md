# Personal API connections

Issue 59 adds owner-scoped API connections to profile settings. A user can name
multiple keys, including several keys from one service, and set an ordered list
of connection/model pairs for text, images, music, sound effects, and video. Each
modality remembers its own `API Craft` or `My API` preference. The generation
confirmation lets the user choose the source before submitting work, except for
video: its source and personal models are configured exclusively in the profile.

Supported services reuse the existing adapters: Gemini, OpenRouter, Stability,
ElevenLabs, and MiniMax. Availability depends on the operation and model; an
image key does not grant text generation, and not every image model supports
editing or reference images. Gemini and OpenRouter also support the
[video workspace](video.md). Arbitrary API URLs and a general OpenAI-compatible
proxy are not supported.

## Access and payment

- Paid Craft models require a paid Craft subscription. Subscriptions are not
  implemented yet, so this route is unavailable. A positive wallet balance,
  staff role, or social channel subscription is not an entitlement.
- Personal routes charge the user's external provider account. They create no
  `GenerationCharge`, do not reserve/capture/release Craft credits, and work with
  an empty or financially frozen Craft wallet. Project permissions and workload
  limits still apply; project credit budgets do not limit external spending.
- Explicitly free Craft functions remain available without a subscription or a
  personal key. Storyboard title/description suggestions use `FREE_TEXT_ROUTES`
  from the text registry. An environment override to a paid model is excluded.
  Unknown pricing, a zero estimate, or rounded cost is not proof of free access.
- A personal quote's zero amount means zero **Craft** credits, not zero external
  cost. The UI identifies the connection/model and external payment separately.

The future subscription integration belongs in
`personal_api.access.has_paid_subscription`; it currently returns false. This
module does not implement subscription checkout, plans, or included credits.

## Configuration

Run `python manage.py migrate` to apply `0069_personal_api_connections` and all
video migrations through `0073_video_source_identifier_length` before serving
the updated frontend. Web and all
generation workers need the same
`PERSONAL_API_ENCRYPTION_KEY`, a Fernet key separate from the Django signing
secret and database. Generate a key with the installed cryptography package:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Provision the result through the deployment secret manager/environment, never
Git. Without a valid key, settings remain readable but connection storage and
personal generation are unavailable. Keep the key backed up separately from
database backups; replacing it makes existing encrypted credentials unreadable.
Re-encryption/key-ring rotation is not part of this release. Restart web and
workers together after configuration or code changes. Existing provider runtime
dependencies (including LiteLLM for text and its image adapter) remain required.

Ciphertexts are bound to the connection, owner, and provider. API responses
expose only the name, provider, last-four-character hint, active state, revision,
and timestamps. Full keys are never returned or stored in browser persistence,
job snapshots, or API examples. Saving a key does not make a paid validation
request and does not guarantee the provider's current permission or quota.

## API and job lifecycle

All routes use normal opaque access authentication via `X-User-Token`.

| Endpoint | Purpose |
|---|---|
| `GET /api/personal-api/settings/` | Own connections, five preferences, supported catalog and storage availability; no provider requests |
| `POST /api/personal-api/video-models/refresh/` | Refresh video model metadata with the owner's first active OpenRouter key; return updated settings |
| `POST /api/personal-api/connections/` | Add a named provider key |
| `PATCH /api/personal-api/connections/{id}/` | Rename, replace the key, or enable/disable the connection |
| `DELETE /api/personal-api/connections/{id}/` | Revoke and erase the secret; remove preference references |
| `PUT /api/personal-api/preferences/{modality}/` | Save the source and ordered connection/model pairs |

Video model refresh does not generate media or use Craft credentials. Failure
returns a provider/catalog error and retains the previous valid catalog. The
settings GET stays offline. Saving or resolving a previously discovered video
route after process restart can reload missing catalog metadata using that
route's own credential; accepted jobs use their pinned capabilities instead.

Text/image/audio generation requests accept `generationSource: platform | personal`; omission
uses the saved modality preference. Free built-in helpers retain their explicit
free route. A selected model starts the user's configured chain at that model;
subsequent compatible, approved routes remain candidates. No suitable route
means the API rejects the request before creating paid work. Video uses a separate
signed preflight with the profile's video source, compatible route chain, input
fingerprint, and disclosed duration; it does not accept `generationSource` or a
personal model override from the video page.

Personal estimates also return a signed `generationAccessToken`, valid for five
minutes. The client sends it with the generation request to preserve the approved
connection/model order even if preferences change in another tab. Changed,
revoked or rotated credentials require a new confirmation; the token contains
only safe references and cannot be used by another owner or modality. Direct
API callers may omit the token to resolve their current saved preferences for
these existing text/image/audio flows. Video always requires its separate
`preflightToken` and an idempotent `clientRequestId`; see [Video generation](video.md).

Enqueue persists `generationAccess` with the owner, source, model/connection
order, and credential revisions, without secret values. Workers recheck the
owner's active state, revocation, and revision before submission. Preferences
changed after enqueue do not replace that snapshot. Key replacement and a
disable/re-enable cycle invalidate old grants; another project member cannot
retry a job using the original actor's key. Account closure wipes secrets and
removes preferences.

Fallback is limited to confirmed rejection before acceptance. An ambiguous
timeout, accepted async job, or malformed paid result does not submit a second
paid request. Accepted audio/video jobs keep polling the same connection/provider.
No personal failure falls back to a platform key or another user's key. A paid
request already accepted by the external provider may still incur a charge
after local cancellation or credential revocation.

## Verification

Automated tests use fictitious keys and fake provider responses. They cover
encryption/ownership, key replacement and revocation, priority and capability
selection, queued job execution, no Craft wallet changes for personal work,
paid Craft rejection without entitlement, and the free storyboard exemption.
No live paid requests are part of the automated checks.
