from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.dashboard_models import (
    AssetType,
    ProjectAsset,
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.reference_library.errors import ReferenceConflict
from w_craft_back.movie.reference_library.lifecycle import (
    cancel_reference_job,
    claim_reference_job,
    fail_reference_job,
    recover_stale_reference_jobs,
    retry_reference_job,
)
from w_craft_back.movie.reference_library.models import (
    ProjectReference,
    ReferenceGenerationJob,
    ReferenceJobStatus,
    ReferenceOperation,
)


class ReferenceLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reference-lifecycle")
        key = UserKey.objects.create(user=self.user)
        self.project = Project.objects.create(
            owner=self.user,
            user=key,
            title="Film",
            format="full-movie",
            annot="",
            desc="",
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.user,
            role=ProjectMemberRole.OWNER,
        )
        self.reference = ProjectReference.objects.create(
            project=self.project,
            title="Medallion",
            category="prop",
            created_by=self.user,
            updated_by=self.user,
        )
        self.job = ReferenceGenerationJob.objects.create(
            project=self.project,
            reference=self.reference,
            actor=self.user,
            operation=ReferenceOperation.GENERATE,
            variant_count=1,
            idempotency_key="lease-test",
            request_fingerprint="a" * 64,
        )

    def test_expired_lease_is_requeued_and_stale_worker_cannot_fail_job(self):
        claimed = claim_reference_job(self.job.id, lease_seconds=30)
        self.assertIsNotNone(claimed)
        ReferenceGenerationJob.objects.filter(pk=self.job.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

        recovered = recover_stale_reference_jobs()

        self.assertIn(self.job.id, recovered["recovered"])
        self.assertFalse(
            fail_reference_job(
                claimed,
                code="STALE",
                detail="must not win",
            )
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ReferenceJobStatus.QUEUED)

    def test_expired_lease_after_provider_started_requires_manual_retry(self):
        claim_reference_job(self.job.id, lease_seconds=30)
        temporary_asset = ProjectAsset.objects.create(
            project=self.project,
            uploaded_by=self.user,
            file="projects/test/reference-crash.png",
            asset_type=AssetType.REFERENCE,
            title="Crash output",
            metadata={"reference_job_id": str(self.job.id)},
        )
        ReferenceGenerationJob.objects.filter(pk=self.job.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
            provider_started_at=timezone.now() - timedelta(seconds=5),
        )

        recovered = recover_stale_reference_jobs()

        self.assertIn(self.job.id, recovered["failed"])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ReferenceJobStatus.FAILED)
        self.assertEqual(self.job.error_code, "IMAGE_PROVIDER_OUTCOME_UNKNOWN")
        self.assertFalse(ProjectAsset.objects.filter(pk=temporary_asset.pk).exists())

    def test_archived_reference_job_cannot_be_retried(self):
        ReferenceGenerationJob.objects.filter(pk=self.job.id).update(
            status=ReferenceJobStatus.FAILED,
        )
        ProjectReference.objects.filter(pk=self.reference.id).update(
            archived_at=timezone.now(),
        )

        with self.assertRaisesMessage(ReferenceConflict, "Archived references"):
            retry_reference_job(self.job.id, actor=self.user)

    def test_failure_after_cancellation_request_finishes_as_cancelled(self):
        claimed = claim_reference_job(self.job.id, lease_seconds=30)
        cancel_reference_job(self.job.id)

        persisted = fail_reference_job(
            claimed,
            code="IMAGE_PROVIDER_BAD_RESPONSE",
            detail="must not overwrite cancellation",
        )

        self.assertFalse(persisted)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ReferenceJobStatus.CANCELLED)
        self.assertEqual(self.job.error_code, "")
