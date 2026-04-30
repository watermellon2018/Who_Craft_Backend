from abc import ABC, abstractmethod


class AIImageProvider(ABC):
    @abstractmethod
    def generate_character_variants(self, job, compiled_prompt, variant_count):
        raise NotImplementedError

    @abstractmethod
    def edit_character_region(self, job, compiled_prompt, variant_count):
        raise NotImplementedError

    @abstractmethod
    def generate_character_sheet(self, job, compiled_prompt):
        raise NotImplementedError


class MockProvider(AIImageProvider):
    model_name = "mock-character-provider"
    model_version = "mvp-1"

    def generate_character_variants(self, job, compiled_prompt, variant_count):
        return self._variants(job, compiled_prompt, variant_count, "initial")

    def edit_character_region(self, job, compiled_prompt, variant_count):
        return self._variants(job, compiled_prompt, variant_count, "edit")

    def generate_character_sheet(self, job, compiled_prompt):
        return self._variants(job, compiled_prompt, 4, "sheet")

    def _variants(self, job, compiled_prompt, variant_count, prefix):
        safe_count = max(3, min(int(variant_count or 4), 4))
        variants = []
        for index in range(safe_count):
            seed = abs(hash(f"{job.job_id}:{index}:{prefix}")) % 100000000
            variants.append(
                {
                    "variant_index": index,
                    "image_url": f"https://placehold.co/768x1024/1b1d22/fab005?text=Character+{index + 1}",
                    "storage_path": f"mock/characters/{job.character_id}/{job.job_id}/{index}.png",
                    "width": 768,
                    "height": 1024,
                    "mime_type": "image/png",
                    "seed": seed,
                    "model_name": self.model_name,
                    "model_version": self.model_version,
                    "prompt": compiled_prompt["positive_prompt"],
                    "negative_prompt": compiled_prompt["negative_prompt"],
                    "metadata": {"provider": "mock", "prefix": prefix},
                }
            )
        return variants


def get_image_provider(name="mock"):
    return MockProvider()

