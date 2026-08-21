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
- storyboard: 25%, from storyboard records accepted for the current scene
  revision;
- video: 35%, from planned video shots with a selected final video asset.

When no significant character has a positive scene weight, character readiness
is `null` and the other weights are normalized over 80%. The existing
`progress.overall`, `script`, `visual`, `audio`, and `postproduction` percentage
fields remain for rolling-deployment compatibility; new clients should use
`progress.readiness`.

## Storyboard and video lifecycle

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
