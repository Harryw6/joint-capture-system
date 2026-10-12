"""Collection logging must not replace the selected upstream teleop behavior."""

import ast
from pathlib import Path
from types import ModuleType

import piper_gamepad_teleop as runner


def test_loader_uses_upstream_teleop_without_custom_wrapper(monkeypatch, tmp_path):
    upstream = ModuleType("main")
    upstream.Teleop = type("UpstreamTeleop", (), {})
    forbidden_wrapper = ModuleType("safe_teleop")

    def forbidden(*args):
        raise AssertionError("rollback must not load the custom controller")

    forbidden_wrapper.safe_controller = forbidden
    monkeypatch.setitem(runner.sys.modules, "main", upstream)
    monkeypatch.setitem(runner.sys.modules, "safe_teleop", forbidden_wrapper)
    monkeypatch.setattr(runner.sys, "path", list(runner.sys.path))
    assert runner.load_controller(tmp_path) is upstream.Teleop
    assert runner.sys.path[0] == str(tmp_path)


def test_loop_uses_upstream_post_iteration_wait():
    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    waits = [node for node in ast.walk(run) if isinstance(node, ast.Call)
             and ast.unparse(node.func) == "pygame.time.wait"]
    assert len(waits) == 1
    assert isinstance(waits[0].args[0], ast.Constant)
    assert waits[0].args[0].value == 5
    assert "next_tick" not in {node.id for node in ast.walk(run) if isinstance(node, ast.Name)}
