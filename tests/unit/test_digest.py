from hazelst import digest
from hazelst.models import Accumulation
from tests.pages import sample_payload


def test_digest_takes_accumulation_fields_it_knows() -> None:
    """The page's accumulation carries `sources` (forecast-verify); the digest does not."""
    payload = sample_payload()
    first = payload.mountains[0].model_copy(
        update={
            "accumulation": Accumulation(
                source="nohrsc",
                last7_in=[0.0, 1.5, None, 0.0, 2.0, 0.0, 0.5],
                season_in=12.0,
                base_in=8.0,
                sources=["nohrsc", "pns"],
            )
        }
    )
    payload = payload.model_copy(update={"mountains": [first, *payload.mountains[1:]]})
    built = digest.from_payload(payload)
    acc = built.mountains[0].accumulation
    assert acc is not None
    assert (acc.source, acc.season_in, acc.base_in) == ("nohrsc", 12.0, 8.0)
    assert acc.last7_in[1] == 1.5
