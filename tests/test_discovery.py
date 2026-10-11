import pytest

from probe.config import eligible, runtime_targets, select_targets, target_apis
from probe.jobs import make_job


@pytest.mark.parametrize("model", [
    "anthropic.claude-opus-4-8", "global.anthropic.claude-sonnet-5-5",
    "us.anthropic.claude-haiku-5-5", "openai.gpt-5.6-sol", "global.openai.gpt-6-astra",
    "openai.gpt-6.1-sol", "anthropic.claude-opus-6",
])
def test_requested_and_future_models_are_eligible(model):
    assert eligible(model)


@pytest.mark.parametrize("model", [
    "anthropic.claude-sonnet-4-6", "anthropic.claude-opus-4-7", "openai.gpt-5.5",
    "global.anthropic.claude-sonnet-4-20250514-v1:0",
    "openai.gpt-oss-120b", "amazon.nova-pro", "some-model",
])
def test_older_and_unrelated_models_are_excluded(model):
    assert not eligible(model)


def test_runtime_does_not_invent_regional_gpt_support():
    targets = runtime_targets("us-east-1", [
        {"modelId": "openai.gpt-6-astra", "inferenceTypesSupported": ["INFERENCE_PROFILE"],
         "responseStreamingSupported": True},
    ], [
        {"inferenceProfileId": "global.openai.gpt-6-astra", "status": "ACTIVE"},
        {"inferenceProfileId": "us.openai.gpt-6-astra", "status": "INACTIVE"},
    ])
    assert len(targets) == 2
    assert all(t.routing == "global" for t in targets)
    assert all(t.model_id == "global.openai.gpt-6-astra" for t in targets)


def test_mantle_closed_weight_gpt_uses_openai_prefix():
    targets = target_apis("us-east-1", "openai.gpt-5.6-sol", "mantle", "regional")
    assert {t.route for t in targets} == {"/openai/v1/responses", "/openai/v1/chat/completions"}


def test_dedup_key_changes_with_slot_input_or_generation_settings():
    target = target_apis("us-east-1", "openai.gpt-6-astra", "mantle", "regional")[0]
    first = make_job(target, 10, 600, 512)
    assert first == make_job(target, 10, 600, 512)
    assert len({first["job_id"], make_job(target, 1, 600, 512)["job_id"],
                make_job(target, 10, 1200, 512)["job_id"], make_job(target, 10, 600, 256)["job_id"]}) == 4


def test_selection_can_limit_model_api_and_routing_without_changing_ids():
    targets = [
        *target_apis("us-east-1", "global.openai.gpt-6-astra", "runtime", "global"),
        *target_apis("us-east-1", "us.openai.gpt-6-astra", "runtime", "geographic"),
        *target_apis("us-east-1", "global.openai.gpt-5.6-sol", "runtime", "global"),
    ]
    selected = select_targets(targets, r"gpt-6-astra$", ("responses",), ("global",))
    assert len(selected) == 1
    assert selected[0].model_id == "global.openai.gpt-6-astra"


@pytest.mark.parametrize("version", ["5", "5-1"])
def test_fable_global_only_keeps_both_runtime_apis_and_other_models(version):
    targets = [
        *target_apis("us-east-1", f"global.anthropic.claude-fable-{version}", "runtime", "global"),
        *target_apis("us-east-1", f"us.anthropic.claude-fable-{version}", "runtime", "geographic"),
        *target_apis("us-east-1", f"anthropic.claude-fable-{version}", "mantle", "regional"),
        *target_apis("us-east-1", "anthropic.claude-opus-5", "mantle", "regional"),
    ]
    selected = select_targets(targets, fable_global_only=True)
    fable = [target for target in selected if "fable" in target.model_id]
    assert len(fable) == 2
    assert {target.api for target in fable} == {"converse", "messages"}
    assert all(target.endpoint == "runtime" and target.routing == "global"
               and target.model_id.startswith("global.") for target in fable)
    assert any(target.endpoint == "mantle" and "opus" in target.model_id for target in selected)


def test_fable_global_only_does_not_fabricate_a_profile_or_fall_back():
    targets = target_apis("ap-northeast-1", "anthropic.claude-fable-5", "mantle", "regional")
    assert select_targets(targets, fable_global_only=True) == []
    assert select_targets(targets, fable_global_only=False) == targets
