from django.db import migrations, models


PROJECT_FORMAT_ALIASES = {
    "full-movie": "feature_film",
    "short-movie": "short_film",
    "short-film": "short_film",
    "marketing": "commercial",
}
CANONICAL_PROJECT_FORMATS = {
    "short_film",
    "feature_film",
    "series",
    "clip",
    "commercial",
    "other",
}
CANONICAL_HAIR_LENGTHS = {"", "bald", "short", "medium", "long"}
HAIR_LENGTH_ALIASES = {
    "buzz": "short",
    "bob": "short",
    "shoulder_length": "medium",
    "very_long": "long",
}


def _unsupported_values(queryset, field: str, accepted_values: set[str]) -> list:
    return list(
        queryset.exclude(**{f"{field}__in": accepted_values})
        .order_by(field)
        .values_list(field, flat=True)
        .distinct()
    )


def normalize_runtime_values(apps, schema_editor) -> None:
    """Normalize known aliases and reject unknown project formats."""

    database = schema_editor.connection.alias
    Project = apps.get_model("w_craft_back", "Project")
    projects = Project.objects.using(database)
    accepted_formats = CANONICAL_PROJECT_FORMATS | set(PROJECT_FORMAT_ALIASES)
    MusicTrack = apps.get_model("w_craft_back", "MusicTrack")
    tracks = MusicTrack.objects.using(database)
    MusicAsset = apps.get_model("w_craft_back", "MusicAsset")
    assets = MusicAsset.objects.using(database)
    ReferenceVersion = apps.get_model("w_craft_back", "ReferenceVersion")
    reference_versions = ReferenceVersion.objects.using(database)
    CharacterAppearance = apps.get_model(
        "w_craft_back",
        "CharacterAppearance",
    )
    appearances = CharacterAppearance.objects.using(database)

    unsupported = {
        "Project.format": _unsupported_values(
            projects,
            "format",
            accepted_formats,
        ),
        "MusicTrack.source": _unsupported_values(
            tracks,
            "source",
            {"manual", "generated", "legacy"},
        ),
        "MusicTrack.unversioned_audio_file": list(
            tracks.filter(active_version__isnull=True)
            .exclude(audio_file__isnull=True)
            .exclude(audio_file="")
            .order_by("pk")
            .values_list("pk", flat=True)
        ),
        "MusicAsset.origin": _unsupported_values(
            assets,
            "origin",
            {"generated", "upload", "legacy"},
        ),
        "MusicAsset.verification_status": _unsupported_values(
            assets,
            "verification_status",
            {"verified", "pending", "missing", "legacy_unverified"},
        ),
        "ReferenceVersion.source_type": _unsupported_values(
            reference_versions,
            "source_type",
            {"upload", "generated", "edit", "legacy"},
        ),
        "CharacterAppearance.hair_length": _unsupported_values(
            appearances,
            "hair_length",
            CANONICAL_HAIR_LENGTHS | set(HAIR_LENGTH_ALIASES),
        ),
    }
    unsupported = {
        field: values for field, values in unsupported.items() if values
    }
    if unsupported:
        raise RuntimeError(
            "Runtime value normalization found unsupported values: "
            f"{unsupported}. Map them to canonical values before retrying."
        )

    for old_value, canonical_value in PROJECT_FORMAT_ALIASES.items():
        projects.filter(format=old_value).update(format=canonical_value)

    tracks.filter(source="legacy").update(source="manual")

    assets.filter(origin="legacy").update(origin="upload")
    assets.filter(verification_status="legacy_unverified").update(
        verification_status="pending"
    )

    reference_versions.filter(source_type="legacy").update(
        source_type="upload"
    )

    for old_value, canonical_value in HAIR_LENGTH_ALIASES.items():
        appearances.filter(hair_length=old_value).update(
            hair_length=canonical_value
        )

    # PostgreSQL defers foreign-key trigger checks until transaction end. Flush
    # them before the following ALTER TABLE operations in this atomic migration.
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0053_remove_project_legacy_fields"),
    ]

    operations = [
        migrations.RunPython(
            normalize_runtime_values,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="characterappearance",
            name="hair_length",
            field=models.CharField(
                blank=True,
                choices=[
                    ("bald", "Bald"),
                    ("short", "Short"),
                    ("medium", "Medium"),
                    ("long", "Long"),
                ],
                default="",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="project",
            name="format",
            field=models.CharField(
                choices=[
                    ("short_film", "Короткометражный фильм"),
                    ("feature_film", "Полнометражный фильм"),
                    ("series", "Сериал"),
                    ("clip", "Клип"),
                    ("commercial", "Реклама"),
                    ("other", "Другое"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="musictrack",
            name="source",
            field=models.CharField(
                choices=[
                    ("manual", "Manual"),
                    ("generated", "Generated"),
                ],
                default="manual",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="musicasset",
            name="origin",
            field=models.CharField(
                choices=[
                    ("generated", "Generated"),
                    ("upload", "Uploaded"),
                ],
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="musicasset",
            name="verification_status",
            field=models.CharField(
                choices=[
                    ("verified", "Verified"),
                    ("pending", "Pending verification"),
                    ("missing", "Missing"),
                ],
                default="verified",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="referenceversion",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("upload", "Upload"),
                    ("generated", "Generated"),
                    ("edit", "Edit"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="characterappearance",
            constraint=models.CheckConstraint(
                check=models.Q(
                    hair_length__in=("", "bald", "short", "medium", "long")
                ),
                name="chk_character_hair_length_canonical",
            ),
        ),
        migrations.AddConstraint(
            model_name="project",
            constraint=models.CheckConstraint(
                check=models.Q(
                    format__in=(
                        "short_film",
                        "feature_film",
                        "series",
                        "clip",
                        "commercial",
                        "other",
                    )
                ),
                name="chk_project_format_canonical",
            ),
        ),
        migrations.AddConstraint(
            model_name="musictrack",
            constraint=models.CheckConstraint(
                check=models.Q(source__in=("manual", "generated")),
                name="chk_music_track_source_canonical",
            ),
        ),
        migrations.AddConstraint(
            model_name="musictrack",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(audio_file__isnull=True)
                    | models.Q(audio_file="")
                    | models.Q(active_version__isnull=False)
                ),
                name="chk_music_track_audio_versioned",
            ),
        ),
        migrations.AddConstraint(
            model_name="musicasset",
            constraint=models.CheckConstraint(
                check=models.Q(origin__in=("generated", "upload")),
                name="chk_music_asset_origin_canonical",
            ),
        ),
        migrations.AddConstraint(
            model_name="musicasset",
            constraint=models.CheckConstraint(
                check=models.Q(
                    verification_status__in=("verified", "pending", "missing")
                ),
                name="chk_music_asset_verification_canonical",
            ),
        ),
        migrations.AddConstraint(
            model_name="referenceversion",
            constraint=models.CheckConstraint(
                check=models.Q(source_type__in=("upload", "generated", "edit")),
                name="chk_reference_version_source_canonical",
            ),
        ),
    ]
