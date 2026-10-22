"""Process exit codes shared by every command; a verification failure is distinct from a command that could not run."""

EXIT_SUCCESS = 0
EXIT_VERIFICATION_FAILED = 1
EXIT_USAGE_ERROR = 2
EXIT_INFRASTRUCTURE_ERROR = 3
EXIT_NOT_FOUND = 4
EXIT_AUTH_ERROR = 5
EXIT_RATE_LIMITED = 6
EXIT_OUTCOME_UNKNOWN = 7
EXIT_QUESTIONS_PENDING = 10
EXIT_INTERRUPTED = 130
EXIT_BROKEN_PIPE = 141
"""128 + SIGPIPE, what a shell reports for a process that wrote to a pipe nothing reads any more."""
