from gift_sniper.floors import FloorCache


class ClientDroppingSomeModels:
    """Simulates the server silently dropping model names it doesn't
    recognize: the first batch call returns 13 of 15 requested keys, then
    per-model retries succeed for one and fail for the other.
    """

    def __init__(self):
        self.calls: list[list[str]] = []

    def model_backgrounds_floors(self, model_names: list[str]):
        self.calls.append(list(model_names))
        if len(model_names) == 15:
            # drop "Model12" and "Model13"
            return {
                "model_backgrounds": {
                    name: {"Copper": "5.0"} for name in model_names if name not in ("Model12", "Model13")
                }
            }
        if model_names == ["Model12"]:
            return {"model_backgrounds": {"Model12": {"Copper": "6.0"}}}
        if model_names == ["Model13"]:
            return {"model_backgrounds": {}}  # still missing on individual retry
        raise AssertionError(f"unexpected call: {model_names}")


def test_batch_missing_two_retries_individually_then_one_still_missing():
    client = ClientDroppingSomeModels()
    cache = FloorCache(client, ttl_sec=600)

    models = [f"Model{i}" for i in range(15)]
    cache.warm(set(models))

    assert len(client.calls) == 1 + 2  # 1 batch + 2 individual retries

    ok_snapshot = cache.snapshot_for("l1", "Model12", "Copper")
    assert ok_snapshot.api_combo_floor_nano is not None
    assert ok_snapshot.floor_skip_reason is None

    missing_snapshot = cache.snapshot_for("l2", "Model13", "Copper")
    assert missing_snapshot.api_combo_floor_nano is None
    assert missing_snapshot.floor_skip_reason == "model_not_returned"

    normal_snapshot = cache.snapshot_for("l3", "Model0", "Copper")
    assert normal_snapshot.api_combo_floor_nano is not None
    assert normal_snapshot.floor_skip_reason is None
