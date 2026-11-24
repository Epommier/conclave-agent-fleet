"""Fixed validation lists and patterns shared by report.py (records writer) and summary.py."""
import re

# The runner's exception type names (harbor 0.26.0, models/trial/error_policy.py).
EXCEPTIONS = frozenset((
    "EnvironmentStartTimeoutError", "SandboxBuildFailedError", "AgentSetupTimeoutError",
    "HealthcheckError", "SandboxLikelyOutOfMemoryError", "MemoryLimitExceededError",
    "SSHAccessError", "GKEExecStreamClosedError", "GKEPodNotFoundError", "RuntimeRequestError",
    "AddTestsDirError", "DownloadVerifierDirError", "ApiError", "ApiRateLimitError",
    "ApiInternalServerError", "ApiOverloadedError", "ApiConnectionClosedError",
    "ApiResponseStalledError", "UnknownApiError", "NetworkConnectionError", "ApiConnectionError",
    "ApiUsageLimitError", "RateLimitError", "InternalServerError", "ServiceUnavailableError",
    "APIConnectionError", "APIError", "Timeout", "UnsupportedModelParametersError",
    "BadRequestError", "UnprocessableEntityError", "UnsupportedParamsError",
    "APIResponseValidationError", "BudgetExceededError", "VerifierTimeoutError",
    "RewardFileNotFoundError", "RewardFileEmptyError", "VerifierOutputParseError",
    "AgentAuthenticationError", "ModelNotFoundError", "ApiProviderResourceNotFoundError",
    "AuthenticationError", "PermissionDeniedError", "NotFoundError",
    "ServiceOperationsUnsupportedError", "VercelScopeError", "DspyImportError",
    "MissingExtraError", "RegradeError", "CancelledError", "NonZeroAgentExitCodeError",
    "AgentTimeoutError", "ContextWindowExceededError", "OutputTokenExceededError",
    "ContextLengthExceededError", "OutputLengthExceededError", "AgentSafetyRefusalError",
    "ContentPolicyViolationError"))
SUBAGENTS = frozenset(["conclave:" + a for a in (
    "dependency-analyzer", "diagnostician", "executor", "mech-executor", "plan-verifier",
    "researcher", "scout", "security-executor", "security-reviewer", "verifier")]
    + ["general-purpose", "Explore", "Plan", "statusline-setup", "claude-code-guide"])
MODELS = frozenset(("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"))
# A tuple: report.py tests a raw TOML value against it, which may be unhashable.
NETWORKS = ("allowlist", "public", "no-network")
ARMS = ("plain", "fleet")
NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}")
VERSION = re.compile(r"[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}")
FLEET = re.compile(r"[0-9a-f]{7,40}")
