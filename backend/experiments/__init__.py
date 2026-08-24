"""AuraBench finite experiment matrix API."""

from .resolve import (
    FileOrLibraryScenarioResolver,
    ScenarioResolver,
    load_and_resolve_matrix,
    load_matrix_file,
    resolve_matrix,
)
from .runner import (
    CellExecutor,
    CellExecutionResult,
    CompletedResultValidator,
    MatrixRunError,
    MatrixRunner,
    MatrixSummary,
    cell_shard_index,
    select_shard,
    summarize_results,
)
from .spec import (
    MATRIX_SCHEMA_VERSION,
    ExactExclusion,
    ExperimentCell,
    MatrixAxes,
    MatrixSpec,
    ResolvedMatrix,
    ScenarioContract,
)

__all__ = [
    "MATRIX_SCHEMA_VERSION",
    "CellExecutor",
    "CellExecutionResult",
    "CompletedResultValidator",
    "ExactExclusion",
    "ExperimentCell",
    "FileOrLibraryScenarioResolver",
    "MatrixAxes",
    "MatrixRunError",
    "MatrixRunner",
    "MatrixSpec",
    "MatrixSummary",
    "ResolvedMatrix",
    "ScenarioContract",
    "ScenarioResolver",
    "cell_shard_index",
    "load_matrix_file",
    "load_and_resolve_matrix",
    "resolve_matrix",
    "select_shard",
    "summarize_results",
]
