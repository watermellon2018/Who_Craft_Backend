# Character 3D persistence and reviewed model publishing

Character Studio keeps 3D edits in `StudioCharacter.model3d_params`, the autofit
flags, and an internal nullable `active_model3d_asset` pointer. Migration `0074`
adds the pointer without modifying existing characters or history. Existing
characters continue to resolve the latest compatible reconstruction until a
reviewed publish or history restore pins an asset.

## API and permissions

The existing endpoints and response shapes are unchanged:

- `GET /api/projects/{project}/characters/{character}/model3d` requires project
  view permission and returns params, autofit flags, and the selected
  reconstruction with freshly signed head/hair URLs.
- `PUT .../model3d` requires edit permission. It replaces the complete parameter
  document using the existing bounded validator and creates a revision in the
  same transaction. Both this route and `POST .../model3d/autofit` lock the
  character row before reading or changing 3D state; revision failures roll
  back the parameter save.
- `GET .../revisions` lists existing history. `POST
  .../revisions/{revision}/restore` requires edit permission and creates a new
  revision; it never rewrites the original history row. The editor must reload
  `GET .../model3d` after restoring to obtain the selected model and new URLs.

The pointer is internal and is not added to the character JSON response. A
compatible pin wins over newer model assets, including when GET ensures a
reconstruction job. The model must belong to the same character and project,
be a ready `model_3d` asset with both head and hair, and match the current
reference fingerprint. If references change, normal resolution ignores the
incompatible pin and selects/queues the current reference set.

## Snapshot and restore contract

All newly created revisions include authoritative current 3D state, even when
the caller supplies a smaller custom snapshot:

```json
{
  "model3d_params": {"eyes": {"eyeSize": 0.2}},
  "model3d_autofit_done": true,
  "model3d_autofit_version": 7,
  "model3d_asset_id": "asset-uuid-or-null",
  "model3d_reconstruction": {
    "asset_id": "asset-uuid",
    "reference_fingerprint": "reference-set-hash",
    "head_storage_path": "private/storage/key/head.glb",
    "hair_storage_path": "private/storage/key/hair.glb",
    "head_sha256": "head-file-hash",
    "hair_sha256": "hair-file-hash"
  }
}
```

`model3d_snapshot()` creates this fragment without signed URLs. Reconstruction
is `null` when no ready compatible asset is available. An explicit null asset
releases the pin and uses normal reconstruction resolution; it does not delete
media or cancel jobs. Parameter saves, autofit and reviewed publishing capture
the complete 3D fragment before mutation as well.

Restore validates params and autofit types, same-character/project ownership,
ready model type, reference compatibility, storage keys, file presence, and
available SHA-256 digests before committing any state. A missing, changed, or
incompatible explicit asset fails atomically with a validation error; restore
never substitutes a newer asset for it. Cross-character revisions are rejected
in the service as well as the HTTP route.

Earlier reviewed snapshots using `model3d_reconstruction.asset_id` (including
angry_dog revision 109) are supported. Truly legacy snapshots with neither
asset key restore the fields they contain and retain the live asset selection
and any absent autofit flags. They cannot recover an asset identity that was
never recorded. Old snapshots/media are not rewritten or backfilled. This
feature does not promise rollback of the whole reference library: explicit
3D restore requires its original reference fingerprint still to be current.

## Immutable generated and reviewed media

New reconstruction jobs use
`character-studio/model3d/{character}/{fingerprint}/{job_uuid}/head.glb` and
`hair.glb`. Retries creating a new job get a new directory. Workers calculate
inside a lease-specific work directory, verify the current lease under a row
lock, then exclusively create the published files. A stale worker cannot
overwrite the current lease's files, and existing published paths are never
reused. The lease work directory lives under `MEDIA_ROOT/model3d-work/` to avoid
overlong Windows paths. Legacy shared fingerprint paths remain readable.
If a process stops after publishing only one component, that job fails safely;
an explicit retry creates a new job/directory instead of overwriting the partial
output.

A reviewed local candidate is published with a separate management command.
This is an operator workflow after visual review, not an automatic similarity
claim or another generation request. It needs the existing backend/PostgreSQL
and private storage configuration, project edit permission for the selected
`UserKey` actor, and local self-contained GLB v2 files. No new dependency or
external provider is required.

```powershell
python manage.py publish_reviewed_model3d `
  --character-id CHARACTER_UUID --actor-id USERKEY_ID `
  --expected-revision 109 --expected-asset-id ORIGINAL_ASSET_UUID `
  --head C:/review/head.glb --hair C:/review/hair.glb `
  --head-sha256 REVIEWED_HEAD_HASH --hair-sha256 REVIEWED_HAIR_HASH `
  --params C:/review/complete-params.json `
  --provenance C:/review/provenance.json
```

The default is a read-only JSON review report. `--params` is the full zone →
parameter document, not a sparse patch or viewer fixture. `--provenance` is an
optional JSON object retained as audit metadata. Add `--apply` to exactly the
reviewed command to publish. Revision/asset guards and checksums are rechecked
on apply, so changed character state or changed files require another review.
The command also validates the original selected model's files.

Apply creates a fresh `appearance-{asset_uuid}` folder, a ready asset derived
from the previous model, and a revision containing before/after state. It pins
the new asset and sets the current autofit version, so an old automatic color
upgrade cannot immediately overwrite the accepted palette. Original files and
asset rows remain available for restore. The GLB validator enforces bounded
size, v2 container metadata and embedded resources; visual/geometry validation
is still the caller's responsibility.

Private media writes cannot participate in a database rollback. A storage/DB
failure may leave an unlinked UUID directory; it is never reused or deleted
automatically. Preserve it for diagnosis and remove it only after confirming
that no asset/revision refers to it. Model files are private generated media
and must not be committed to Git.

## Verification

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test w_craft_back.character_studio.test_model3d_history
python manage.py test w_craft_back.character_studio.test_model3d_reconstruction
python manage.py test w_craft_back.character_studio.tests.Model3DStageTests
python manage.py test w_craft_back.character_studio.tests.Model3DAutofitTests
python manage.py test w_craft_back.character_studio.tests.RevisionTests
python scripts/check_openapi_contract.py
```

Tests use a separate PostgreSQL test database and temporary media. The history
test covers authenticated GET/PUT/history/restore and actual signed head/hair
delivery through the normal URL configuration and middleware, in addition to
restore failure rollback, legacy snapshots, immutable retry paths, stale
workers and reviewed publishing. This is distinct from testing a user's live
browser session or validating resemblance in the 3D renderer.
