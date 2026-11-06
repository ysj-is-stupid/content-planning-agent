"""Small synthetic, versioned material catalog."""

from __future__ import annotations

from pydantic import Field

from .canonical import fingerprint, normalize_phrase
from .schemas import ContentFormat, StrictModel

CATALOG_VERSION = "r1"


class Topic(StrictModel):
    topic_id: str
    aliases: tuple[str, ...]


class Material(StrictModel):
    material_id: str
    topic_id: str
    title: str
    summary: str
    formats: tuple[ContentFormat, ...]
    license: str = "synthetic-demo-only"
    version: str = CATALOG_VERSION

    @property
    def content_hash(self) -> str:
        return fingerprint(self)


TOPICS = (
    Topic(topic_id="product_launch", aliases=("新品发布", "产品发布", "product launch")),
    Topic(topic_id="eco_campaign", aliases=("环保活动", "绿色生活", "eco campaign")),
    Topic(topic_id="community_event", aliases=("社区活动", "社区市集", "community event")),
)

MATERIALS = (
    Material(material_id="launch-01", topic_id="product_launch", title="新品亮点", summary="合成素材：介绍新品功能与适用场景。", formats=(ContentFormat.VIDEO, ContentFormat.IMAGE_TEXT)),
    Material(material_id="launch-02", topic_id="product_launch", title="用户体验", summary="合成素材：用户体验片段与反馈。", formats=(ContentFormat.VIDEO, ContentFormat.IMAGE_TEXT)),
    Material(material_id="eco-01", topic_id="eco_campaign", title="绿色生活", summary="合成素材：低碳行动与参与方式。", formats=(ContentFormat.VIDEO, ContentFormat.IMAGE_TEXT)),
    Material(material_id="community-01", topic_id="community_event", title="社区市集", summary="合成素材：活动场地与报名信息。", formats=(ContentFormat.VIDEO, ContentFormat.IMAGE_TEXT)),
)

BY_ID = {material.material_id: material for material in MATERIALS}


def resolve_topic(text: str) -> str | None:
    normalized = normalize_phrase(text)
    for topic in TOPICS:
        if normalized in (normalize_phrase(alias) for alias in topic.aliases):
            return topic.topic_id
    return None


def search(topic_id: str, query: str = "", selected_format: ContentFormat | None = None) -> tuple[Material, ...]:
    keyword = normalize_phrase(query)
    topic = next((item for item in TOPICS if item.topic_id == topic_id), None)
    mentions_topic = bool(topic and any(normalize_phrase(alias) in keyword for alias in topic.aliases))
    terms = tuple(part for part in keyword.split() if part)
    return tuple(
        material for material in MATERIALS
        if material.topic_id == topic_id
        and (selected_format is None or selected_format in material.formats)
        and (
            not keyword
            or mentions_topic
            or keyword in normalize_phrase(material.title + " " + material.summary)
            or any(term in normalize_phrase(material.title + " " + material.summary) for term in terms)
        )
    )
