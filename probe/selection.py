def default_policy():
    return {"revision": 0, "auto_add": True, "overrides": {}}


def enabled(target, policy):
    return policy.get("overrides", {}).get(target.key, policy.get("auto_add", True))
