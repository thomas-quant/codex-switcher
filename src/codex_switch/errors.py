from __future__ import annotations


class CodexSwitchError(RuntimeError):
    """Base exception for user-facing CLI failures."""


class InvalidAliasError(CodexSwitchError):
    pass


class AliasAlreadyExistsError(CodexSwitchError):
    pass


class SnapshotNotFoundError(CodexSwitchError):
    pass


class UnsafeAccountDirectoryError(CodexSwitchError):
    pass


class UnsafeSnapshotEntryError(CodexSwitchError):
    pass


class ActiveAliasRemovalError(CodexSwitchError):
    pass


class CodexProcessRunningError(CodexSwitchError):
    pass


class StateFileError(CodexSwitchError):
    pass


class LoginCaptureError(CodexSwitchError):
    pass
