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
    path("projects/<int:project_id>/characters/<uuid:character_id>/revisions", views.revisions_collection),
    path(
        "projects/<int:project_id>/characters/<uuid:character_id>/revisions/<uuid:revision_id>/restore",
        views.restore_revision,
    ),
]
