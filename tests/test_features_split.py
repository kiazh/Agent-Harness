"""Test that features.py split into domain modules preserves all methods."""
from __future__ import annotations


def test_all_feature_methods_accessible_after_split():
    """All methods from the original features.py must be importable from sub-modules."""
    from ah.gateway.features import METHODS

    expected = {
        "session.fork", "session.delete", "session.rename", "session.setGoal",
        "session.search", "session.export",
        "context.get", "context.compress",
        "memory.list", "memory.search", "memory.add", "memory.forget",
        "memory.pending", "memory.approve", "memory.reject",
        "memory.approveAll", "memory.rejectAll", "memory.stats",
        "skills.list", "skills.show", "skills.learn", "skills.delete", "skills.curator",
        "config.get", "profile.get", "profile.set", "profile.list", "status",
        "agents.list", "agents.save", "agents.delete", "agents.run", "agents.history",
        "jobs.create", "jobs.list", "jobs.setEnabled", "jobs.delete",
    }
    assert set(METHODS) == expected


def test_submodules_importable():
    """Each domain module must be importable."""
    from ah.gateway.features import sessions, memory, skills, agents, jobs, config
    assert hasattr(sessions, "session_fork")
    assert hasattr(memory, "memory_list")
    assert hasattr(skills, "skills_list")
    assert hasattr(agents, "agents_list")
    assert hasattr(jobs, "jobs_create")
    assert hasattr(config, "config_get")


def test_register_still_works():
    """register() must still add all methods to a gateway."""
    from ah.gateway.features import METHODS, register
    from ah.gateway.server import Gateway

    gw = Gateway(lambda frame: None)
    register(gw)
    assert set(METHODS) <= set(gw._methods)


def test_coerce_config_value_preserved():
    """coerce_config_value must still be accessible."""
    from ah.gateway.features import coerce_config_value
    assert coerce_config_value("max_iterations", "7") == 7
    assert coerce_config_value("verbose", "off") is False
