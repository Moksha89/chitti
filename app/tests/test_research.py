from datetime import UTC, datetime

from chitti.research import (
    ResearchFact,
    ResearchPackageDocument,
    ResearchPackageSource,
    canonical_package,
)


def test_research_package_canonical_content_is_order_stable() -> None:
    retrieved_at = datetime(2026, 8, 17, tzinfo=UTC)
    first = ResearchFact(
        key="fixture_date",
        value="2026-09-14",
        source_url="https://example.test/fixture",
        retrieved_at=retrieved_at,
        content_digest="a" * 64,
    )
    second = ResearchFact(
        key="venue",
        value="Dubai International Stadium",
        source_url="https://example.test/venue",
        retrieved_at=retrieved_at,
        content_digest="b" * 64,
    )
    package = ResearchPackageDocument(
        fixture=[first],
        teams=[second],
    )
    source = ResearchPackageSource(
        url="https://example.test/fixture",
        retrieved_at=retrieved_at,
        content_digest="a" * 64,
    )
    assert canonical_package(package, [source]) == canonical_package(
        ResearchPackageDocument.model_validate(package.model_dump(mode="json")),
        [ResearchPackageSource.model_validate(source.model_dump(mode="json"))],
    )
