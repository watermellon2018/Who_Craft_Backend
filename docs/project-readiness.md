# Project readiness

`GET /api/projects/{project_id}/dashboard/` calculates project readiness from
current domain data whenever the dashboard is loaded. No cached percentage or
background worker is required.

The ratio-based contract is returned under `progress.readiness`:

- scenario: 20%, from non-empty scene descriptions, legacy script text,
  substantive script blocks, or scene notes; the legacy default heading
  template alone is empty, while a user-authored heading is content;
- characters: 20%, weighted by distinct scenario appearances and limited to
  significant characters (`replica_count > 5`, `scene_count >= 2`, or a usable
  ready identity asset);
- storyboard: 25%, from either legacy storyboard assets accepted for the
  current scene revision or structured storyboards whose shots are all complete
  for the current scene revision;
- video: 35%, from planned video shots with a selected final video asset.

When no significant character has a positive scene weight, character readiness
is `null` and the other weights are normalized over 80%. The existing
`progress.overall`, `script`, `visual`, `audio`, and `postproduction` percentage
fields remain for rolling-deployment compatibility; new clients should use
`progress.readiness`.

## Video preparation

`GET /api/projects/{project_id}/video/preparation/` recalculates the checklist
that guards entry to video generation. The project is ready only when all three
conditions are true:

- there are no significant screenplay characters without a visible logical
  Character Studio character;
- there are no empty scenes under the same content rule used by script
  readiness;
- every scene has either a legacy storyboard accepted for its current scene
  revision or a structured storyboard whose shots are complete for that
  revision.

The response contains the actionable missing-character and empty-scene lists,
missing or stale storyboard scene details, and a `taskCount`. Incomplete
storyboard coverage contributes one task regardless of the number of affected
scenes. A project with no scenes is not ready. The dashboard exposes only the
compact `progress.readiness.videoPreparation` status; clients must re-read the
dedicated endpoint before entering generation because project data can change
after the dashboard was loaded.

The checklist is informational and requires project view permission. A future
generation mutation must repeat the prerequisite and generation-permission
checks atomically; the GET response is not an authorization token.

## Project roadmap

`GET /api/projects/{project_id}/dashboard/` also returns a top-level `roadmap`
object. It is a versioned, copy-free navigation contract derived from the same
current domain data as readiness; it does not store a mutable "current stage".
The steps are returned in this stable order: script, characters, references,
music, storyboard, and video.

Script, characters, storyboard, and video are required. References and music
are explicitly optional: their state is still reported, but they never block a
required step and are never selected as `nextAction`. Script, characters, and
storyboard may be started independently. An untouched storyboard is reported as
`not_started`; once any storyboard exists it is `in_progress`. Stale storyboard
data is exposed as a warning blocker without locking the stage or replacing its
working state. Video becomes actionable after the script, characters, and
current storyboard are ready. A character step already in progress is
recommended before an untouched script so a visual-first workflow can continue
naturally.

Each step exposes only stable identifiers and data: `state`, integer metrics,
machine-readable blockers, progress, and its application route. Labels and
localized explanations belong to clients. `needs_attention` identifies work
that needs intervention, while storyboard warnings preserve the storyboard's
ordinary `not_started` or `in_progress` state. `blocked` means a required
dependency is not ready yet. The supported v1 blocker codes are:

- `incompleteScenes` and `missingCharacters`;
- `generationFailed` for an unresolved optional reference or music job;
- `scriptNotReady`, `charactersNotReady`, and `storyboardNotReady`;
- `staleStoryboards` when accepted storyboard data no longer matches the scene
  revision.

Character progress uses one count-based denominator: saved project characters
plus significant screenplay characters that do not have a logical character
yet. The ready count includes only visible characters with a ready identity
asset, so the percentage always matches the reported total/ready pair and any
`missingCharacters` blocker.

`nextAction` considers required available steps only. It prioritizes
`needs_attention`, then `in_progress`, then `not_started`, using the stable
required order to resolve ties. Storyboard has no navigation dependency and can
be recommended while preparation is still underway; video remains excluded
until its required inputs are ready. `nextAction` is `null` once every required
step is ready.

## Storyboard and video lifecycle

- Structured storyboard authoring is available under
  `/api/projects/{project_id}/storyboard/`. It stores shots, camera keyframes,
  transitions, and asynchronous still-image revisions; see
  [Structured Storyboard](storyboard.md). A structured storyboard is ready only
  when every shot has ready START and END camera frames for the current scene
  revision.
- `PUT /api/projects/{project_id}/scenes/{scene_id}/storyboard/` registers a
  storyboard asset and its source scene revision.
- `POST .../storyboard/confirm/` confirms the current revision and rejects a
  concurrent edit with HTTP 409.
- `GET|POST /api/projects/{project_id}/video-shots/` lists or plans shots.
- `PATCH|DELETE /api/projects/{project_id}/video-shots/{shot_id}/` selects a
  final asset, edits the plan, or removes a planned shot.

Viewing follows project view permission. Registering or confirming a storyboard
and changing video shots requires project content-edit permission. Assets are
always restricted to the same project and to the expected storyboard/video
type.

The dashboard warns about storyboard scenes that need review but intentionally
does not offer blind confirmation without a storyboard review surface. Existing
generic storyboard/video `ProjectAsset` rows cannot be safely assigned to a
scene or shot automatically; they count only after a lifecycle link is recorded.
