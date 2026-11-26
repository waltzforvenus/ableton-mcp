"""Legacy-compatible views carrying explicit JSON-safe model metadata.

These types retain str/list behavior for the legacy SDK adapter. Structured
transport consumes metadata directly instead of reverse-parsing prose or trying
to serialize SDK Image objects as JSON.
"""


class MetadataText(str):
    def __new__(cls, text, metadata):
        instance = super().__new__(cls, text)
        instance.metadata = metadata
        return instance


class MetadataBlocks(list):
    def __init__(self, blocks, metadata):
        super().__init__(blocks)
        self.metadata = metadata
