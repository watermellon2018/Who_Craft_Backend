"""Video billing uses the accepted quote and never charges personal API jobs."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from w_craft_back.services.video_generation import registry

from .models import GenerationCharge
from .pricing import GenerationEstimate
from .services import (
    MONEY_QUANTUM,
    GenerationPriceUnavailable,
    capture_generation,
    money,
    release_generation,
    reserve_generation,
)

if TYPE_CHECKING:
    from w_craft_back.movie.video.models import VideoGenerationJob
    from w_craft_back.services.video_generation.contracts import VideoModelSpec


def estimate_video_generation(
    spec: VideoModelSpec, duration: int, resolution: str = "720p",
) -> GenerationEstimate:
    """Fail closed when the provider has no comparable, verified USD quote."""
    cost = registry.estimate_cost(spec, duration, resolution)
    if cost is None or not cost.is_finite() or cost <= 0:
        raise GenerationPriceUnavailable(
            "Провайдер не сообщил тариф выбранной видеомодели. "
            "Генерация через API Craft пока недоступна."
        )
    # Preserve the existing ledger's precision without turning tiny paid quotes
    # into a free platform call.
    amount = max(money(cost), MONEY_QUANTUM)
    snapshot = {
        "currency": "USD", "source": spec.provider,
        "modelKey": spec.key, "modelName": spec.provider_model,
        "durationSeconds": duration, "resolution": resolution,
        "estimatedCost": str(amount), "reservationAmount": str(amount),
        "markup": "0", "creditUsdRate": "1",
    }
    return GenerationEstimate(
        provider=spec.provider, model_key=spec.key,
        model_name=spec.provider_model, currency="USD",
        estimated_cost=amount, reservation_amount=amount,
        pricing_source=spec.provider, prompt_tokens_estimate=0,
        snapshot=snapshot,
    )


def _is_platform(job: VideoGenerationJob) -> bool:
    # A missing payer snapshot is not permission to debit a wallet.
    return job.provider_snapshot.get("generationAccess", {}).get(
        "source"
    ) == "platform"


def reserve_video_generation(job: VideoGenerationJob):
    if not _is_platform(job):
        return None
    pricing = job.provider_snapshot.get("pricing") or {}
    try:
        estimated = Decimal(str(pricing["estimatedCost"]))
        reserved = Decimal(str(pricing["reservationAmount"]))
        valid = (estimated.is_finite() and reserved.is_finite()
                 and estimated > 0 and reserved >= estimated)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        valid = False
    if not valid or job.actor is None:
        raise GenerationPriceUnavailable("Для видео нет подтверждённой стоимости.")
    return reserve_generation(
        user=job.actor, project=job.project, domain="video", job_id=str(job.id),
        provider=job.provider_snapshot["provider"],
        model_name=job.provider_snapshot["modelKey"],
        estimated_cost=estimated, reservation_amount=reserved,
        pricing_snapshot=dict(pricing.get("snapshot") or {}),
    )


def capture_video_generation(
    job: VideoGenerationJob, provider_usage: dict[str, Any] | None = None,
):
    if not _is_platform(job):
        return None
    charge = GenerationCharge.objects.filter(
        domain="video", job_id=str(job.id),
    ).first()
    if charge is None:
        return None
    # Neither adapter currently promises a final cost in its download response.
    # Do not reinterpret arbitrary provider metadata as a billable amount.
    return capture_generation(
        domain="video", job_id=str(job.id), actual_cost=charge.estimated_cost,
        cost_is_estimate=True,
        provider_usage={
            "costSource": "enqueue-estimate",
            "selectedProvider": job.provider_snapshot["provider"],
            "selectedModel": job.provider_snapshot["modelKey"],
        },
    )


def release_video_generation(job: VideoGenerationJob, reason: str = ""):
    if not _is_platform(job):
        return None
    return release_generation(domain="video", job_id=str(job.id), reason=reason)
