from django.urls import path

from w_craft_back.character_studio import views

urlpatterns = [
    path("projects/<int:project_id>/characters", views.characters_collection),
    path("projects/<int:project_id>/characters/", views.characters_collection),
    path("projects/<int:project_id>/characters/<uuid:character_id>", views.character_detail),
    path("projects/<int:project_id>/characters/<uuid:character_id>/", views.character_detail),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/generate-initial-variants",
        views.generate_initial_variants,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/generate-edit-variants",
        views.generate_edit_variants,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/zone-edit",
        views.zone_edit,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/zone-edit/",
        views.zone_edit,
    ),
    path("generation-jobs/<uuid:job_id>", views.get_generation_job),
    path("projects/<int:project_id>/characters/<uuid:character_id>/apply-variant", views.apply_variant),
    path("projects/<int:project_id>/characters/<uuid:character_id>/lock-identity", views.lock_identity),
    path("projects/<int:project_id>/characters/<uuid:character_id>/outfits", views.outfits_collection),
    path("projects/<int:project_id>/characters/<uuid:character_id>/outfits/", views.outfits_collection),
    path("projects/<int:project_id>/characters/<uuid:character_id>/outfits/<uuid:outfit_id>", views.outfit_detail),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/outfits/<uuid:outfit_id>/set-default",
        views.set_default_outfit,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/outfits/<uuid:outfit_id>/generate-variants",
        views.generate_outfit_variants,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/outfits/<uuid:outfit_id>/upload-reference",
        views.upload_outfit_reference,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/outfits/<uuid:outfit_id>/delete-reference",
        views.delete_outfit_reference,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/clothing-references",
        views.upload_clothing_reference,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/clothing-references/",
        views.upload_clothing_reference,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/clothing-references/<uuid:asset_id>",
        views.delete_clothing_reference,
    ),
    path("projects/<int:project_id>/characters/<uuid:character_id>/revisions", views.revisions_collection),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/revisions/<uuid:revision_id>/restore",
        views.restore_revision,
    ),
    # References stage --------------------------------------------------------
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/references",
        views.references_collection,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/references/",
        views.references_collection,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/references/generate",
        views.references_generate,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/references/generate-missing",
        views.references_generate_missing,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/references/upload",
        views.references_upload,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/references/readiness",
        views.references_readiness,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/references/checklist",
        views.references_checklist,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/references/proceed-to-3d",
        views.references_proceed_to_3d,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/references/<uuid:reference_id>/correct",
        views.references_correct,
    ),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/references/<uuid:reference_id>/make-primary",
        views.references_make_primary,
    ),
]
