# Structured Storyboard

The Storyboard module turns a screenplay scene into an ordered, editable shot
plan. It stores scene shots, START/END and optional intermediate keyframes,
camera intent, inferred transitions, continuity references, and immutable still
image generation revisions. It does not generate or assemble video.

## API and permissions

Structured routes live under `/api/projects/{project_id}/storyboard/`. The scene
list is a lightweight progress response; the scene detail returns the full
workspace and its resolved scene context. `POST .../scenes/{scene_id}/` creates
the scene aggregate idempotently. Shot, keyframe, camera, transition, reference,
preview, and generation routes are documented in `docs/openapi.json`.

Project view permission is sufficient for reads and preview. Project content
edit permission is required for mutations. Image generation additionally uses
the central generation policy and credit reservation. Every related character,
location, visual reference, keyframe, and generated asset is resolved through
the project scope; a UUID from another project is never accepted or disclosed.

The existing `/api/projects/{project_id}/scenes/{scene_id}/storyboard/` endpoint
remains compatible with legacy storyboard assets. Its GET response includes the
structured workspace when available, and POST initializes the structured form.

## Authoring lifecycle

Creating a shot atomically creates its START (`position=0`) and END
(`position=1`) keyframes and the transition between them. Intermediate frames
must have a unique position strictly between those boundaries. Editing camera
intent recalculates only adjacent inferred transitions; a user transition
override remains explicit. Shot readiness is computed from current data: both
boundary frames need camera intent and a ready selected image revision.

`GET .../suggest-shots/` returns one option per allowlisted text model, its
selected provider route and availability, scene context, and a best-effort USD
estimate based on LiteLLM token and route-specific price metadata. The estimate
is not a reservation or invoice and can be absent when pricing metadata is
unavailable; a different provider's tariff is never substituted. The option's
`id` remains the concrete LiteLLM route to send as `model` in the POST, so the
confirmed provider is not reselected between estimation and generation.
The legacy `POST .../suggest-shots/` asks the selected allowlisted model for strict
JSON-schema output synchronously and returns an unpersisted proposal. Interactive
editors use the durable `shot-list-jobs` endpoints below instead. Structured
shot mutation endpoints remain available for the render pipeline. Provider
identifiers are validated against the resolved scene context before the
proposal is returned.

Both requests accept optional `language=ru|en` (query parameter for GET, JSON
field for POST). The explicit interface language takes precedence; otherwise the
saved `UserProfile.language` is used, with `ru` as the fallback. The prompt and
per-field schema explicitly require generated titles and descriptions in that
language. `content_language` is not used. Entity IDs, proper names, source text,
and verbatim dialogue remain unchanged. Existing English drafts are not silently
translated or regenerated; changing language affects subsequent generations.

The shot-list request explicitly requires entity IDs, not display names. Its
JSON schema limits the number of shots to the requested `maxShots` and restricts
character, location, and visual-asset references to IDs in this scene. Empty
entity catalogs require empty reference arrays or a null location. Responses
still undergo server-side field and entity validation because provider schema
enforcement varies.

Each proposed shot also returns `source_segment_ids`. The server divides the
canonical scene text into deterministic sentence/line segments while retaining
every character and whitespace. The model selects only IDs from the supplied
segments; unknown IDs, duplicate IDs within one shot, and missing attribution
for nonempty text are rejected. Several shots may cite the same segment, for
example an establishing shot and a reaction. Source quotations are never
accepted from the model.

The proposal's `source` contains the authorized `scene_id`, `scene_version`,
SHA-256 `content_hash` of the full canonical UTF-8 text, ordered `{id, text}`
segments, and `truncated`. Joining all segment text without separators exactly
reconstructs the source snapshot. This preserves the server's existing text
precedence: `script_text`, then script block text, then description/notes.
Only the first 20,000 source characters are sent to the model, with a segment
boundary inserted at the limit; those segments replace the raw scene text in
the prompt rather than duplicating it. If `truncated=true`, the response still
includes the complete scene, but shots can reference only the supplied prefix.
The estimate and generation both use the same segmented prompt construction.

This attribution belongs to the proposal and its durable editor working copy.
It does not change structured shot mutation endpoints. Clients retain the
snapshot with the draft and highlight referenced segments in the full screenplay
viewer. If the scene later
changes, label the snapshot as the generation-time source rather than attaching
its IDs to new text. Older proposals have no reliable source attribution; do
not infer exact quotations from generated descriptions or regenerate silently.

## Durable editor working copies

### Shot-list generation survives navigation and closed tabs

Migration `0066_storyboard_shot_list_jobs` adds durable scene text-generation
jobs and request receipts. `POST .../scenes/{scene_id}/shot-list-jobs/` requires
generation permission and accepts `requestId` (UUID), optional `model`, `maxShots`,
`language`, and `estimatedSeconds` (5–3600, default 60). It commits the job and
immediately returns HTTP 202 without calling the provider. The estimate is only
for the progress display; it is neither a deadline nor a guarantee. The request
captures the canonical source, resolved scene context, concrete provider/model
route, language, and current saved editor revision. Clients should finish any
pending editor save before enqueueing.

Retries with the same project/requester/request UUID and identical parameters
return the same job. Changed parameters for that UUID are rejected. Concurrent
requests for a scene reuse its active job; each request receipt remembers the
shared job even after it finishes, preventing a late HTTP retry from paying for
the same work again. A new request UUID after completion intentionally starts
new work. Browser navigation does not cancel a job.

`GET .../shot-list-jobs/` returns `{jobs:[...]}` with the latest job for every
scene. `GET .../shot-list-jobs/{job_id}/` also reads older jobs. Both require
project view permission, scope all identifiers to that project, and expose
`queued`, `running`, `succeeded`, or `failed` status, start/finish timestamps,
the display estimate, safe error code, and the saved editor proposal. Proposal
IDs, START/END keyframes, transitions, source attribution, and generated language
are stable across reconnects. No provider credentials or raw errors are exposed.

The worker commits each validated result before attempting to adopt it as the
scene's editor draft. Adoption is automatic only while the captured editor
revision, source version/hash, and content-edit permission still match, and the
proposal has not been dismissed. A reset, concurrent edit, source change, or
permission change leaves the completed result saved with `resultState=pending`;
it does not overwrite newer work. `resultState=applied` includes the resulting
`appliedRevision`. A process interruption after result persistence but before
adoption also leaves a recoverable pending result.

`POST .../shot-list-jobs/{job_id}/apply/` accepts `{expectedRevision, mutationId}`
and requires content-edit permission. A stale revision returns the existing
`STORYBOARD_DRAFT_CONFLICT` (409). Replaying an already applied job returns its
recorded result without overwriting any subsequent edits.
`POST .../shot-list-jobs/{job_id}/dismiss/` is idempotent, requires content-edit
permission, and marks the result dismissed without deleting it. Dismissing active
work suppresses adoption but does not cancel a provider call. Dismissed proposals
cannot be applied; start new work if needed.

The existing `run_generation_worker --queue storyboard` (or `--queue all`)
process handles these text jobs alongside keyframe images. Restart the worker
after upgrading, and run migrations before enabling the new client. Web and
worker must share PostgreSQL and the same text-provider settings. Without a
running worker, requests remain queued. Text execution reuses
`STORYBOARD_JOB_LEASE_SECONDS`; the actual lease is always at least the shot-list
provider timeout plus 60 seconds. A lost lease before the provider starts may be
retried up to three claims. After the provider starts, an expired lease becomes
`STORYBOARD_AI_OUTCOME_UNKNOWN`; there is no automatic paid retry. Provider errors
are saved as failed jobs, so returning users see the outcome without resubmission.

### Saved editor data

Migration `0065_storyboard_editor_drafts` creates one `SceneStoryboardEditorDraft`
per scene. It stores the editable shot list, manual selections, camera intent,
keyframe/reference metadata, and authoring stage (`selection`, `builder`, or
`editor`). Partially marked-up scenes with confirmed shots and an empty shot list
are valid drafts. An unconfirmed browser text selection or unfinished form is
not included until the user adds the shot.
These working copies are shared by project collaborators; they do not overwrite
structured shot records, generated stills, immutable revisions, or legacy assets.
Deleting a scene cascades to its working copy.

`GET .../editor-drafts/` requires project view permission and returns
`{userId, canEdit, drafts}` without creating records. Each entry is
`{sceneId, revision, payload}`. `PUT .../scenes/{scene_id}/editor-draft/` requires
content edit permission and accepts `{expectedRevision, mutationId, payload}`.
The payload is `{schemaVersion:1, stage, shots}`. Use revision zero on a first
save. Writes lock the scene row, so concurrent first saves and subsequent edits
cannot silently overwrite one another. A stale revision returns HTTP 409,
`STORYBOARD_DRAFT_CONFLICT`, and `errors.currentRevision`. An identical retry of
the most recent mutation UUID returns the original successful entry without
incrementing its revision; the same UUID with changed content is rejected.
Older retries after another mutation are handled as stale writes.

Payload validation whitelists nested fields, limits each copy to 2 MiB JSON,
500 shots, and 100 keyframes per shot. Shot/keyframe IDs must be unique, shot
order contiguous, and transitions must reference frames in their own shot.
Editor composition coordinates and dimensions use percentages from 0 to 100
and are saved without conversion; keyframe positions remain fractions from 0 to 1.
The editor must strip image URLs and normalize transient `loading` status to
`idle` before saving. Embedded `data:`, `blob:`, `mock:` media and credential URLs
are not accepted. Metadata is JSON only; media binaries remain in private storage.

Each shot can retain an optional `source` with `document`, `segmentIds`, optional
`origin` (`manual` or `ai`), and optional `ranges`. Manual ranges index Unicode
code points in the joined snapshot text, with an inclusive start and exclusive
end; overlapping ranges and sharing source text among shots are allowed. The
server checks bounds, SHA-256, scene identity, and current-version canonical text.
Older internally valid source snapshots survive script changes and must be shown
as older text by the client. Saving/reopening working copies never invokes AI.

Failures emit `storyboard_shot_list_failed` under the same `request_id` as the
HTTP response. Safe fields include `model`, `provider`, `error_code`, the
categorical `status`, `exception_type`, and, when available, the upstream
`status_code`. Categories distinguish timeout, rate limiting, provider rejection,
truncated/refused/invalid JSON, invalid shot count/fields, unknown entities, and
invalid source segment references.
Provider exception messages, response bodies, scene text, and credentials are
never included. Old generic `django_request_error` lines alone cannot establish
the provider failure's cause.

The HTTP status remains 502 for provider failures. The client distinguishes
`STORYBOARD_AI_TIMEOUT`, `STORYBOARD_AI_RATE_LIMITED`,
`STORYBOARD_AI_PROVIDER_REJECTED`, and `STORYBOARD_AI_BAD_RESPONSE`; unknown
provider errors retain `STORYBOARD_AI_FAILED`. No automatic paid retry is made.

Text model names and default routes live separately from the image and audio
registries in `w_craft_back/services/text_generation/registry.py`. The initial
catalog contains Gemini 2.5 Flash (Google or OpenRouter), Qwen3 235B A22B 2507,
DeepSeek V3.2, and GPT-5.4 mini (the latter three through OpenRouter). Models
without configured credentials remain visible but disabled. An explicit
route allowlist can restrict or extend this catalog without changing the UI.

For each model, the server picks the first available route in configuration
order, with `STORYBOARD_SHOT_LIST_MODEL` first. This is an explicit priority,
not automatic cheapest-provider selection. For the default Gemini model,
Google is preferred when both keys are configured; OpenRouter is selected when
only its key is configured. The dialog shows the selected provider separately
from the model. If that exact route becomes unavailable after confirmation,
generation fails instead of silently choosing another route or mock output.

OpenRouter calls require parameter support (`provider.require_parameters=true`)
for the strict JSON-schema request. App/SDK retries and OpenRouter provider
fallbacks are disabled for each paid request. OpenRouter still
selects the initial compatible endpoint behind its route; this catalog does
not pin or quote a specific upstream hosting endpoint. Schema support and
actual usage can differ by endpoint, so responses are still validated on the
server and the displayed cost remains an estimate. Catalog availability is a
configuration check, not a paid provider health probe.

## Still-image generation

`POST .../keyframes/{keyframe_id}/generate/` and `/regenerate/` require an
`Idempotency-Key`. They create immutable request snapshots and queued generation
revisions; provider calls never run in the HTTP request. Poll
`GET .../generations/{generation_id}/` for status. Workspace keyframes also
expose their selected `image`, newest `latestGeneration`, and any queued or
running `activeGeneration`, so reconnecting does not hide regeneration
progress. A successful worker result is stored in private project media and
becomes the keyframe's current revision only if its immutable input is still
current. Later edits leave the revision in history and make the previous image
report `outdated=true`.

Run the durable queue with:

```bash
python manage.py run_generation_worker --queue storyboard
```

The web and worker processes must share PostgreSQL, private media storage, image
provider configuration, and credit settings. Recovery uses database leases and
safe retry semantics. Known provider rejections and pre-provider failures
release their reservation. A received result or truly ambiguous timeout is
conservatively captured at the reserved estimate and marked as estimated usage
for audit and reconciliation.

## Configuration and limits

- `STORYBOARD_SHOT_LIST_MODEL` selects the preferred default LiteLLM text route.
  The default `gemini-2.5-flash` is normalized to
  `gemini/gemini-2.5-flash` and uses the direct Gemini API. To route the request
  through OpenRouter, set an explicit LiteLLM model such as
  `openrouter/google/gemini-2.5-flash` and configure `OPENROUTER_API_KEY`.
- `STORYBOARD_SHOT_LIST_MODELS` optionally overrides the comma-separated route
  allowlist and its priority order. Omit it to use the text catalog. Existing
  explicit allowlists are respected and do not automatically gain new models;
  remove the override or append desired routes to enable them. The configured
  default is always included, but duplicate routes/models are collapsed for UI.
  Gemini routes require `GEMINI_API_KEY`; OpenRouter routes require
  `OPENROUTER_API_KEY`. Other provider prefixes are shown as unavailable until
  Storyboard adds an explicit credential mapping for them. LiteLLM must be
  installed from `requirements.txt`.

The added OpenRouter route IDs are:

- `openrouter/qwen/qwen3-235b-a22b-2507`
- `openrouter/deepseek/deepseek-v3.2`
- `openrouter/openai/gpt-5.4-mini`

Model IDs and JSON-schema capability were checked against the
[Qwen model](https://openrouter.ai/qwen/qwen3-235b-a22b-2507),
[DeepSeek model](https://openrouter.ai/deepseek/deepseek-v3.2), and
[GPT model](https://openrouter.ai/openai/gpt-5.4-mini) catalog entries on
2026-08-31. See OpenRouter's
[structured-output limitations](https://openrouter.ai/docs/guides/features/structured-outputs).
Adding another model on a supported route requires a text registry entry;
adding an entirely new provider also requires its credential/adapter support.

Other operational settings:

- `STORYBOARD_SHOT_LIST_TIMEOUT_SECONDS` limits a Shot List provider call.
- `STORYBOARD_SHOT_LIST_THROTTLE_RATE` limits Shot List start requests per
  authenticated user (default `10/min`).
- `STORYBOARD_PROVIDER_TIMEOUT_SECONDS` limits a still-image provider call.
- `STORYBOARD_JOB_LEASE_SECONDS` controls durable worker lease recovery.
- Still images reuse the image provider registry and Reference Library routing
  settings; mock output requires the existing explicit mock opt-in.

The module intentionally has no video generation or timeline renderer.
Preview data is a storyboard projection
for clients to render; it is not a generated video file.
