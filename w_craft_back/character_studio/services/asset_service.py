from w_craft_back.character_studio.repositories.repositories import AssetRepository


class CharacterAssetService:
    def __init__(self, repository=None):
        self.assets = repository or AssetRepository()

    def save_asset(self, character, asset_type, **payload):
        return self.assets.create(
            character=character,
            project=character.project,
            user=character.user,
            asset_type=asset_type,
            **payload,
        )

    def get_asset(self, asset_id):
        return self.assets.get(asset_id=asset_id)

    def delete_asset(self, asset):
        self.assets.delete(asset)

    def mark_as_primary(self, asset):
        return self.assets.mark_as_primary(asset)

