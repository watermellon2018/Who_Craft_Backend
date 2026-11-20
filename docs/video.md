# Video generation

The project's **Create video** workspace generates one clip for a storyboard
plan. The shared project header and links to characters, visual references,
script, and storyboard let the user return to missing source material.

## Main flow and access

1. Prepare the script, required characters, and **all plans of the selected
   scene**. Other scenes may remain unfinished. Each plan needs a description,
   positive duration, and a valid initial frame. A configured final frame must
   also be ready; otherwise it is optional.
2. Select a plan and enter its motion/camera instruction. This does not rewrite
   the script or storyboard. The initial and optional final frame condition the
   video; intermediate keyframes are not submitted.
3. Set the video API source in **Profile → Personal API → Video**. Craft mode
   exposes the supported model selector on the video page. Personal mode uses
   the ordered connection/model preferences from the profile and has no source
   switch or personal model selector on the video page.
4. Review the effective duration before creating the clip. The closest supported
   duration is selected, preferring the longer value on a tie and respecting
   model/resolution restrictions. The storyboard duration is never changed.
5. The worker submits the job, polls it, and stores the completed video privately.
   Browse versions, then explicitly select the result to use for the plan.
   Completion or previewing another version does not change the selected result.

Project `VIEW` permits history and playback, `RUN_GENERATION` permits creation,
and `EDIT_CONTENT` permits motion edits and final selection. Workers recheck
membership, account activity, and personal credential grants. Losing source
readiness does not hide previously generated videos from authorized viewers.

Characters and visual references are included as contextual descriptions and
marked `context_only`. Their images are not silently combined with first/last
frame mode. There is no clip assembly, transition generation, video extension,
automatic trimming, or interpolation through intermediate keyframes.

## Sources, history, and concurrency

Saved storyboard editor drafts are the primary source. Structured storyboard
plans are supported when no editor draft exists; an old combined storyboard
image is not split into invented plans. Input frames must resolve to successful
generations and assets belonging to the project, rather than arbitrary URLs or
client-supplied readiness flags.

Each job pins its source fingerprint, conditions, assets, model capabilities,
effective parameters, and approved credential references. Keys and signed
provider download URLs are not stored in those snapshots. Protected input-asset
links keep historical and running jobs' inputs available.

Changes to the relevant script, character appearance, references, or storyboard
mark existing results stale without deleting history or changing the final
selection. Removed plans retain their video history in the archived-plan list.
Results produced while sources change retain their original conditions.

Preflight is signed for ten minutes and bound to actor, project, conditions, and
API route. Changed inputs or preferences require a fresh preflight. Job creation
uses an actor/project-scoped UUID request ID: replaying the same request returns
the existing job, while changing its payload is a conflict. A plan may have at
most one job in `queued` or `processing`. A new request while that job is active
returns HTTP 409 with `VIDEO_GENERATION_ACTIVE`; replaying the original request
with the same UUID and payload still returns its existing job. Motion edits and
selection use `expectedVideoShotVersion` to reject concurrent overwrites.

## API

Authenticate with `X-User-Token`. Routes below are under
`/api/projects/{project_id}/video/`; see [OpenAPI](openapi.json) for exact schemas.

| Method and path | Purpose |
|---|---|
| `GET workspace/` | Scenes, readiness, plans, versions, and archived plans |
| `POST options/` | Model compatibility, effective duration, blockers, preflight |
| `GET jobs/` | Recent jobs, optionally filtered by `sceneId` |
| `POST jobs/` | Accept `preflightToken` and `clientRequestId`; return HTTP 202 |
| `GET jobs/{job_id}/` | Poll one job and obtain fresh private playback URLs |
| `PATCH shots/{shot_id}/` | Save motion text with optimistic version checking |
| `POST shots/{shot_id}/select/` | Explicitly select a completed result |

Video options reject a request-level source override. Personal mode also rejects
a page-level model override. Playback uses the existing signed private-media
gateway and byte-range support.

## Deployment and providers

Apply all migrations through `0073_video_source_identifier_length` with
`python manage.py migrate` (including video migrations `0070`–`0073`), then
restart web and workers on the same code, PostgreSQL database, and private media
storage. Start a dedicated worker or include video in `--queue all`:

```bash
python manage.py run_generation_worker --queue video
```

No Redis, Celery, or additional provider SDK is required. The adapters use the
existing HTTP dependency. Configure `GEMINI_API_KEY` and/or `OPENROUTER_API_KEY`
for Craft credentials. Personal requests use the actor's encrypted profile key
exclusively; web and workers must share `PERSONAL_API_ENCRYPTION_KEY` as described
in [Personal API connections](personal-api.md).

| Setting | Default | Purpose |
|---|---|---|
| `VIDEO_HTTP_TIMEOUT_SECONDS` | `45` | Provider HTTP read timeout |
| `VIDEO_DOWNLOAD_TIMEOUT_SECONDS` | `180` | Bounded result download |
| `VIDEO_MAX_DOWNLOAD_BYTES` | `134217728` | Adapter download limit (128 MiB); storage has a separate 100 MiB limit |
| `VIDEO_JOB_LEASE_SECONDS` | `300` | Worker lease; kept above the download deadline |
| `VIDEO_DOWNLOAD_ALLOWED_HOSTS` | empty | Additional exact HTTPS CDN hostnames |

Official provider API hosts and `storage.googleapis.com` are permitted by
default. Redirects are bounded and credentials are never forwarded to a CDN.
The adapter bounds downloads at 128 MiB by default. Private project storage
accepts only structurally validated `video/mp4` files up to 100 MiB; raising the
download limit does not raise this storage limit. WebM output is not supported
for persistence in this version.

Gemini includes documented Veo 3.1, Fast, and Lite models. OpenRouter video model
metadata is discovered from its video catalog, validated, and cached. An outage
retains a prior valid catalog or the conservative documented Veo bootstrap.
Unknown capabilities are not assumed. Model capabilities are pinned in each
accepted job so worker restart does not depend on its local catalog cache.
Profile settings remain readable without provider requests. The explicit
`POST /api/personal-api/video-models/refresh/` action discovers additional video
models using the owner's first active OpenRouter key and returns updated
settings. An error leaves the prior valid catalog intact. Missing dynamic model
metadata is reloaded with the route owner's key when saving or resolving a
stored preference after process restart; it never uses another user's key.

Paid Craft generation requires the future subscription entitlement and a known
price; neither wallet funds nor a social subscription replaces these checks.
Subscriptions are currently unavailable, and ambiguous provider prices are not
converted into Craft charges. Personal jobs incur provider charges but no Craft
credit reservations or deductions.

Fallback uses only already approved compatible personal routes with the same
duration, aspect ratio, and resolution after a confirmed rejection before
acceptance. Once accepted, polling and download continue on the same operation.
A submission timeout or uncertain paid submission becomes `outcome_unknown` and
is not automatically resubmitted. Accepted operations have a 24-hour deadline
from provider submission. Once it expires, the worker stops automatic polling
and marks the job `outcome_unknown` with `VIDEO_OPERATION_TIMEOUT`, retaining the
operation ID and any Craft credit reservation for reconciliation. This is not
proof that the provider failed or did not charge; the worker never issues a
replacement POST automatically. Personal jobs have no Craft reservation to
retain. Workers use durable leases and fenced updates; accepted operations can
resume after worker restart within that deadline.

## Verification and provider references

Provider tests mock HTTP responses; no live paid generation is part of automated
verification. Tests cover request schemas, conditional durations, catalog
validation, credential isolation, rejection versus uncertain acceptance,
download boundaries, and video profile routing. Domain tests cover readiness,
permissions, immutable inputs, idempotency, versions, and worker recovery.

- [Gemini Veo guide](https://ai.google.dev/gemini-api/docs/veo?hl=en)
- [Official Gemini REST image serialization](https://github.com/googleapis/python-genai/blob/v2.25.0/google/genai/models.py#L2691)
- [OpenRouter video generation](https://openrouter.ai/docs/guides/overview/multimodal/video-generation)
- [OpenRouter video model catalog](https://openrouter.ai/docs/api/api-reference/video-generation/list-all-video-generation-models)
- [OpenRouter first/last frame data URLs](https://github.com/OpenRouterTeam/skills/blob/main/skills/openrouter-video/SKILL.md#L70-L73)
