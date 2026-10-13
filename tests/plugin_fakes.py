"""Tiny plugin doubles shared by plugin contract tests."""

from __future__ import annotations

from typing import Any

from src.plugin_system.contracts import PluginConfig, PluginSession, Request, Result, StageId
from src.plugin_system.context import PluginContext


class FakeFactory:
    def __init__(
        self,
        stage: StageId,
        result: Result | None = None,
        error: Exception | None = None,
    ):
        self.stage = stage
        self.result = result
        self.error = error
        self.created: list[FakeSession] = []

    def create_session(self, config: PluginConfig, context: PluginContext) -> "FakeSession":
        session = FakeSession(self.result, self.error, context)
        self.created.append(session)
        return session


class FakeSession:
    def __init__(self, result: Result | None, error: Exception | None, context: PluginContext):
        self.result = result
        self.error = error
        self.context = context
        self.validated: list[Request] = []
        self.ran: list[Request] = []
        self.close_count = 0

    def validate(self, request: Request) -> None:
        self.validated.append(request)

    def run(self, request: Request) -> Result:
        self.ran.append(request)
        self.context.check_cancelled()
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("fake session needs a result")
        return self.result

    def close(self) -> None:
        self.close_count += 1
